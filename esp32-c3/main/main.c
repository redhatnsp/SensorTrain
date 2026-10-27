#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#include <stdbool.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_err.h"
#include "nvs_flash.h"
#include "esp_timer.h"

#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"

#include "host/ble_hs.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/util/util.h"
#include "os/os_mbuf.h"
#include "sensortrain_hubs.h"

static int requested_speed = 0;

static int64_t last_heartbeat_us = 0;

#define HEARTBEAT_TIMEOUT_US 1000000


static bool ble_ready = false;
static uint8_t own_addr_type;

/*
 * SensorTrain v1.2 - independent hub runtime state.
 *
 * BLE connections, GATT handles, and battery
 * information must never be shared between hubs.
 */
typedef struct {
    const sensortrain_hub_config_t *config;

    ble_addr_t address;

    bool found;
    bool connected;
    bool connecting;

    uint16_t conn_handle;

    uint16_t service_start;
    uint16_t service_end;
    uint16_t characteristic_handle;
    uint16_t cccd_handle;

    bool gatt_ready;
    bool notifications_ready;

    int battery_percent;
    int applied_speed;
    bool motor_port0_present;
} sensortrain_hub_state_t;

static sensortrain_hub_state_t
    hub_states[SENSORTRAIN_HUB_COUNT];

static void initialize_hub_states(void)
{
    memset(hub_states, 0, sizeof(hub_states));

    for (int i = 0; i < SENSORTRAIN_HUB_COUNT; i++) {
        hub_states[i].config = &sensortrain_hub_configs[i];
        hub_states[i].battery_percent = -1;
        hub_states[i].applied_speed = 0;
    }
}

static ble_addr_t hub_addr;
static bool hub_found = false;
static bool hub_connected = false;
static bool hub_connecting = false;
static uint16_t hub_conn_handle;

static uint16_t lego_service_start = 0;
static uint16_t lego_service_end = 0;
static uint16_t lego_characteristic_handle = 0;
static uint16_t lego_cccd_handle = 0;

static bool lego_gatt_ready = false;
static bool lego_notifications_ready = false;

/* SENSORTRAIN_HUB_BATTERY_V1 */
static int hub_battery_percent = -1;



/* LEGO service 1623 */
static const ble_uuid128_t lego_service_uuid =
    BLE_UUID128_INIT(
        0x23,0xD1,0xBC,0xEA,
        0x5F,0x78,
        0x23,0x16,
        0xDE,0xEF,
        0x12,0x12,
        0x23,0x16,0x00,0x00
    );

/* LEGO characteristic 1624 */
static const ble_uuid128_t lego_characteristic_uuid =
    BLE_UUID128_INIT(
        0x23,0xD1,0xBC,0xEA,
        0x5F,0x78,
        0x23,0x16,
        0xDE,0xEF,
        0x12,0x12,
        0x24,0x16,0x00,0x00
    );


static void print_ble_addr(const ble_addr_t *addr)
{
    printf("%02X:%02X:%02X:%02X:%02X:%02X",
           addr->val[5],
           addr->val[4],
           addr->val[3],
           addr->val[2],
           addr->val[1],
           addr->val[0]);
}



/* ---------------------------------------------------------
 * LEGO Hub battery monitoring
 *
 * Hub Property:
 *   0x06 = Battery Voltage [%]
 *
 * Operations:
 *   0x02 = Enable Updates
 *   0x05 = Request Update
 *   0x06 = Upstream Update
 * --------------------------------------------------------- */

/*
 * Validate the hub identity for asynchronous battery
 * request callbacks.
 */
static bool battery_hub_valid(
    uint16_t conn_handle,
    const sensortrain_hub_state_t *hub)
{
    return hub != NULL &&
           hub->connected &&
           hub->conn_handle == conn_handle &&
           hub->characteristic_handle != 0;
}


static int battery_request_write_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    struct ble_gatt_attr *attr,
    void *arg)
{
    sensortrain_hub_state_t *hub =
        (sensortrain_hub_state_t *)arg;

    if (!battery_hub_valid(conn_handle, hub)) {
        printf("ERROR BATTERY REQUEST HUB MISMATCH\n");
        return 0;
    }

    if (error->status != 0) {
        printf("%s: ERROR BATTERY REQUEST status=%d\n",
               hub->config->role, error->status);
    }

    return 0;
}


static int battery_enable_write_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    struct ble_gatt_attr *attr,
    void *arg)
{
    sensortrain_hub_state_t *hub =
        (sensortrain_hub_state_t *)arg;

    if (!battery_hub_valid(conn_handle, hub)) {
        printf("ERROR BATTERY ENABLE HUB MISMATCH\n");
        return 0;
    }

    if (error->status != 0) {
        printf("%s: ERROR BATTERY ENABLE status=%d\n",
               hub->config->role, error->status);
        return 0;
    }

    printf("%s: HUB BATTERY UPDATES ENABLED\n",
           hub->config->role);

    uint8_t request_battery[] = {
        0x05, 0x00, 0x01, 0x06, 0x05
    };

    int rc = ble_gattc_write_flat(
        conn_handle,
        hub->characteristic_handle,
        request_battery,
        sizeof(request_battery),
        battery_request_write_cb,
        hub
    );

    if (rc != 0) {
        printf("%s: ERROR BATTERY REQUEST START rc=%d\n",
               hub->config->role, rc);
    }

    return 0;
}


static void enable_hub_battery_updates(
    sensortrain_hub_state_t *hub)
{
    if (hub == NULL ||
        !hub->connected ||
        hub->characteristic_handle == 0) {

        printf("ERROR BATTERY MONITOR NOT READY\n");
        return;
    }

    uint8_t enable_updates[] = {
        0x05, 0x00, 0x01, 0x06, 0x02
    };

    int rc = ble_gattc_write_flat(
        hub->conn_handle,
        hub->characteristic_handle,
        enable_updates,
        sizeof(enable_updates),
        battery_enable_write_cb,
        hub
    );

    if (rc != 0) {
        printf("%s: ERROR BATTERY ENABLE START rc=%d\n",
               hub->config->role, rc);
    }
}


static int notification_enable_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    struct ble_gatt_attr *attr,
    void *arg)
{
    sensortrain_hub_state_t *hub =
        (sensortrain_hub_state_t *)arg;

    if (hub == NULL ||
        !hub->connected ||
        hub->conn_handle != conn_handle) {

        printf("ERROR NOTIFICATION CALLBACK HUB MISMATCH\n");
        return 0;
    }

    if (error->status == 0) {

        hub->notifications_ready = true;

        printf("%s: LEGO NOTIFICATIONS ENABLED\n",
               hub->config->role);

        if (hub == &hub_states[HUB_LOCOMOTIVE]) {

            /*
             * Preserve the existing status protocol
             * and locomotive battery monitoring.
             */
            lego_notifications_ready = true;

            printf("LEGO NOTIFICATIONS ENABLED\n");

            enable_hub_battery_updates(hub);

            printf("UNPLUG AND REPLUG THE TRAIN MOTOR NOW\n");
        }
        else {
            enable_hub_battery_updates(hub);
        }
    }
    else {

        hub->notifications_ready = false;

        if (hub == &hub_states[HUB_LOCOMOTIVE]) {
            lego_notifications_ready = false;
        }

        printf("%s: ERROR ENABLING NOTIFICATIONS status=%d\n",
               hub->config->role,
               error->status);
    }

    return 0;
}


static int descriptor_discovery_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    uint16_t chr_val_handle,
    const struct ble_gatt_dsc *dsc,
    void *arg)
{
    sensortrain_hub_state_t *hub =
        (sensortrain_hub_state_t *)arg;

    if (hub == NULL ||
        !hub->connected ||
        hub->conn_handle != conn_handle) {

        printf("ERROR DESCRIPTOR CALLBACK HUB MISMATCH\n");
        return 0;
    }

    if (error->status == 0 && dsc != NULL) {

        if (ble_uuid_cmp(
                &dsc->uuid.u,
                BLE_UUID16_DECLARE(
                    BLE_GATT_DSC_CLT_CFG_UUID16)) == 0) {

            hub->cccd_handle = dsc->handle;

            /* Temporary locomotive compatibility. */
            if (hub == &hub_states[HUB_LOCOMOTIVE]) {
                lego_cccd_handle = hub->cccd_handle;
            }

            printf("%s: LEGO CCCD FOUND handle=%u\n",
                   hub->config->role,
                   hub->cccd_handle);
        }

        return 0;
    }

    if (error->status == BLE_HS_EDONE) {

        if (hub->cccd_handle == 0) {
            printf("%s: ERROR LEGO_CCCD_NOT_FOUND\n",
                   hub->config->role);
            return 0;
        }

        uint8_t enable_notifications[2] = {
            0x01, 0x00
        };

        int rc = ble_gattc_write_flat(
            conn_handle,
            hub->cccd_handle,
            enable_notifications,
            sizeof(enable_notifications),
            notification_enable_cb,
            hub
        );

        if (rc != 0) {
            printf("%s: ERROR CCCD_WRITE rc=%d\n",
                   hub->config->role, rc);
        }

        return 0;
    }

    printf("%s: ERROR DESCRIPTOR_DISCOVERY status=%d\n",
           hub->config->role, error->status);

    return 0;
}


static int characteristic_discovery_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    const struct ble_gatt_chr *chr,
    void *arg)
{
    sensortrain_hub_state_t *hub =
        (sensortrain_hub_state_t *)arg;

    if (hub == NULL ||
        !hub->connected ||
        hub->conn_handle != conn_handle) {

        printf("ERROR CHARACTERISTIC CALLBACK HUB MISMATCH\n");
        return 0;
    }

    if (error->status == 0 && chr != NULL) {

        hub->characteristic_handle = chr->val_handle;

        /* Temporary locomotive compatibility. */
        if (hub == &hub_states[HUB_LOCOMOTIVE]) {
            lego_characteristic_handle =
                hub->characteristic_handle;
        }

        printf("%s: LEGO CHARACTERISTIC FOUND handle=%u\n",
               hub->config->role,
               hub->characteristic_handle);

        return 0;
    }

    if (error->status == BLE_HS_EDONE) {

        if (hub->characteristic_handle == 0) {
            printf("%s: ERROR LEGO_CHARACTERISTIC_NOT_FOUND\n",
                   hub->config->role);
            return 0;
        }

        hub->gatt_ready = true;

        if (hub == &hub_states[HUB_LOCOMOTIVE]) {
            lego_gatt_ready = true;
        }

        printf("%s: LEGO GATT READY\n",
               hub->config->role);

        printf("%s: SEARCHING FOR NOTIFICATION DESCRIPTOR...\n",
               hub->config->role);

        int rc = ble_gattc_disc_all_dscs(
            conn_handle,
            hub->characteristic_handle,
            hub->service_end,
            descriptor_discovery_cb,
            hub
        );

        if (rc != 0) {
            printf("%s: ERROR DESCRIPTOR_DISCOVERY_START rc=%d\n",
                   hub->config->role, rc);
        }

        return 0;
    }

    printf("%s: ERROR CHARACTERISTIC_DISCOVERY status=%d\n",
           hub->config->role,
           error->status);

    return 0;
}


static int service_discovery_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    const struct ble_gatt_svc *service,
    void *arg)
{
    sensortrain_hub_state_t *hub =
        (sensortrain_hub_state_t *)arg;

    if (hub == NULL ||
        !hub->connected ||
        hub->conn_handle != conn_handle) {

        printf("ERROR SERVICE CALLBACK HUB MISMATCH\n");
        return 0;
    }

    if (error->status == 0 && service != NULL) {

        hub->service_start = service->start_handle;
        hub->service_end = service->end_handle;

        /*
         * Temporary locomotive compatibility.
         */
        if (hub == &hub_states[HUB_LOCOMOTIVE]) {
            lego_service_start = hub->service_start;
            lego_service_end = hub->service_end;
        }

        printf(
            "%s: LEGO SERVICE FOUND start=%u end=%u\n",
            hub->config->role,
            hub->service_start,
            hub->service_end
        );

        return 0;
    }

    if (error->status == BLE_HS_EDONE) {

        if (hub->service_start == 0) {
            printf("%s: ERROR LEGO_SERVICE_NOT_FOUND\n",
                   hub->config->role);
            return 0;
        }

        printf("%s: SEARCHING FOR LEGO CHARACTERISTIC...\n",
               hub->config->role);

        int rc = ble_gattc_disc_chrs_by_uuid(
            conn_handle,
            hub->service_start,
            hub->service_end,
            &lego_characteristic_uuid.u,
            characteristic_discovery_cb,
            hub
        );

        if (rc != 0) {
            printf(
                "%s: ERROR CHARACTERISTIC_DISCOVERY_START rc=%d\n",
                hub->config->role, rc
            );
        }

        return 0;
    }

    printf("%s: ERROR SERVICE_DISCOVERY status=%d\n",
           hub->config->role, error->status);

    return 0;
}


static void start_lego_gatt_discovery(
    sensortrain_hub_state_t *hub)
{
    if (hub == NULL || !hub->connected) {
        printf("ERROR GATT_DISCOVERY_NO_CONNECTED_HUB\n");
        return;
    }

    /*
     * Reset only this hub's discovery state.
     */
    hub->service_start = 0;
    hub->service_end = 0;
    hub->characteristic_handle = 0;
    hub->cccd_handle = 0;
    hub->gatt_ready = false;
    hub->notifications_ready = false;

    /*
     * Temporary compatibility with the existing
     * locomotive-only GATT callbacks.
     *
     * These shared variables will be removed as the
     * remaining callbacks are migrated.
     */
    if (hub == &hub_states[HUB_LOCOMOTIVE]) {
        lego_service_start = 0;
        lego_service_end = 0;
        lego_characteristic_handle = 0;
        lego_cccd_handle = 0;
        lego_gatt_ready = false;
        lego_notifications_ready = false;
    }

    printf("%s: SEARCHING FOR LEGO SERVICE...\n",
           hub->config->role);

    int rc = ble_gattc_disc_svc_by_uuid(
        hub->conn_handle,
        &lego_service_uuid.u,
        service_discovery_cb,
        hub
    );

    if (rc != 0) {
        printf("%s: ERROR SERVICE_DISCOVERY_START rc=%d\n",
               hub->config->role, rc);
    }
}



/* ---------------------------------------------------------
 * AUTO-RECOVERY SUPPORT
 * --------------------------------------------------------- */

static void start_ble_scan(void);
static void stop_all_connected_motors(const char *reason);
static void connect_to_configured_hub(sensortrain_hub_id_t id);

static bool auto_scan_task_pending = false;

static bool any_hub_missing(void)
{
    for (int i = 0; i < SENSORTRAIN_HUB_COUNT; i++) {
        if (!hub_states[i].connected &&
            !hub_states[i].connecting) {
            return true;
        }
    }
    return false;
}

static bool connection_in_progress(void)
{
    for (int i = 0; i < SENSORTRAIN_HUB_COUNT; i++) {
        if (hub_states[i].connecting) {
            return true;
        }
    }
    return false;
}


static void auto_scan_task(void *arg)
{
    /*
     * Wait briefly before rescanning.  This gives NimBLE time
     * to completely finish a disconnect or failed connection.
     */
    vTaskDelay(pdMS_TO_TICKS(1000));

    auto_scan_task_pending = false;

    if (ble_ready &&
        any_hub_missing() &&
        !connection_in_progress() &&
        !ble_gap_disc_active()) {

        printf("AUTO: SEARCHING FOR SENSORTRAIN HUBS...\n");
        start_ble_scan();
    }

    vTaskDelete(NULL);
}

static void schedule_auto_scan(void)
{
    if (auto_scan_task_pending ||
        !any_hub_missing() ||
        connection_in_progress()) {

        return;
    }

    auto_scan_task_pending = true;

    xTaskCreate(
        auto_scan_task,
        "hub_auto_scan",
        3072,
        NULL,
        5,
        NULL
    );
}

static int ble_gap_event(struct ble_gap_event *event, void *arg)
{
    switch (event->type) {

        case BLE_GAP_EVENT_DISC: {

            struct ble_hs_adv_fields fields;

            if (ble_hs_adv_parse_fields(
                    &fields,
                    event->disc.data,
                    event->disc.length_data) != 0) {
                return 0;
            }

            printf("BLE ");
            print_ble_addr(&event->disc.addr);
            printf(" RSSI=%d", event->disc.rssi);

            if (fields.name != NULL && fields.name_len > 0) {

                printf(" NAME=\"%.*s\"",
                       fields.name_len,
                       (const char *)fields.name);

                const sensortrain_hub_config_t *loco =
                    &sensortrain_hub_configs[HUB_LOCOMOTIVE];
                const sensortrain_hub_config_t *cab =
                    &sensortrain_hub_configs[HUB_CABOOSE];

                bool loco_name =
                    fields.name_len == strlen(loco->name) &&
                    memcmp(fields.name, loco->name,
                           fields.name_len) == 0;

                bool cab_name =
                    fields.name_len == strlen(cab->name) &&
                    memcmp(fields.name, cab->name,
                           fields.name_len) == 0;

                bool locomotive_matches =
                    loco_name &&
                    memcmp(event->disc.addr.val,
                           loco->address, 6) == 0;

                bool caboose_matches =
                    cab_name &&
                    memcmp(event->disc.addr.val,
                           cab->address, 6) == 0;

                if (locomotive_matches) {

                    hub_states[HUB_LOCOMOTIVE].address =
                        event->disc.addr;
                    hub_states[HUB_LOCOMOTIVE].found = true;

                    memcpy(&hub_addr,
                           &event->disc.addr,
                           sizeof(hub_addr));

                    hub_found = true;

                    printf("  <-- ST-LOCOMOTIVE");

                    /*
                     * We found the one specific SensorTrain hub.
                     * Stop scanning and connect automatically.
                     */
                    if (!hub_states[HUB_LOCOMOTIVE].connected &&
                        !hub_states[HUB_LOCOMOTIVE].connecting) {

                        if (ble_gap_disc_active()) {
                            ble_gap_disc_cancel();
                        }

                        connect_to_configured_hub(HUB_LOCOMOTIVE);
                    }
                }
                else if (caboose_matches) {

                    hub_states[HUB_CABOOSE].address =
                        event->disc.addr;
                    hub_states[HUB_CABOOSE].found = true;

                    if (!hub_states[HUB_CABOOSE].connected &&
                        !hub_states[HUB_CABOOSE].connecting &&
                        !connection_in_progress()) {

                        printf("  <-- ST-CABOOSE CONNECTING");

                        connect_to_configured_hub(HUB_CABOOSE);
                    }
                    else {

                        printf("  <-- ST-CABOOSE DISCOVERED");
                    }
                }
                else if (loco_name || cab_name) {

                    printf("  <-- NAME/ADDRESS MISMATCH");
                }
            }

            printf("\n");
            break;
        }


        case BLE_GAP_EVENT_DISC_COMPLETE:

            printf("SCAN COMPLETE\n");

            if (any_hub_missing() &&
                !connection_in_progress()) {

                schedule_auto_scan();
            }

            break;


        case BLE_GAP_EVENT_CONNECT: {

            sensortrain_hub_state_t *hub =
                (sensortrain_hub_state_t *)arg;

            /*
             * Accept only our two configured hub contexts.
             */
            if (hub != &hub_states[HUB_LOCOMOTIVE] &&
                hub != &hub_states[HUB_CABOOSE]) {

                printf("ERROR UNEXPECTED CONNECTION CONTEXT\n");

                if (event->connect.status == 0) {
                    ble_gap_terminate(
                        event->connect.conn_handle,
                        BLE_ERR_REM_USER_CONN_TERM
                    );
                }

                break;
            }

            hub->connecting = false;

            if (event->connect.status == 0) {

                hub->connected = true;
                hub->conn_handle =
                    event->connect.conn_handle;

                /*
                 * Temporary compatibility with the
                 * existing locomotive motor protocol.
                 */
                if (hub == &hub_states[HUB_LOCOMOTIVE]) {
                    hub_connecting = false;
                    hub_connected = true;
                    hub_conn_handle = hub->conn_handle;
                }

                printf("%s: HUB CONNECTED handle=%u\n",
                       hub->config->role,
                       hub->conn_handle);

                if (hub == &hub_states[HUB_LOCOMOTIVE]) {
                    printf("HUB CONNECTED handle=%u\n",
                           hub->conn_handle);
                }

                start_lego_gatt_discovery(hub);

                /*
                 * Continue discovering any missing hub.
                 */
                schedule_auto_scan();
            }
            else {

                hub->connected = false;
                hub->found = false;
                hub->conn_handle = 0;
                hub->gatt_ready = false;
                hub->notifications_ready = false;
                hub->battery_percent = -1;
                hub->applied_speed = 0;

                printf("%s: HUB CONNECTION FAILED status=%d\n",
                       hub->config->role,
                       event->connect.status);

                if (hub == &hub_states[HUB_LOCOMOTIVE]) {

                    hub_connecting = false;
                    hub_connected = false;
                    hub_found = false;

                    schedule_auto_scan();
                }

                /*
                 * Failed caboose connection: require a
                 * fresh movement command and retry discovery.
                 */
                requested_speed = 0;
                schedule_auto_scan();
            }

            break;
        }


        case BLE_GAP_EVENT_DISCONNECT: {

            uint16_t lost_handle =
                event->disconnect.conn.conn_handle;

            sensortrain_hub_state_t *hub = NULL;

            /*
             * Identify the disconnected hub by its actual
             * BLE connection handle, not its advertised name.
             */
            for (int i = 0; i < SENSORTRAIN_HUB_COUNT; i++) {
                if (hub_states[i].connected &&
                    hub_states[i].conn_handle == lost_handle) {

                    hub = &hub_states[i];
                    break;
                }
            }

            if (hub == NULL) {
                printf("UNTRACKED HUB DISCONNECT handle=%u\n",
                       lost_handle);
                break;
            }

            printf("%s DISCONNECTED handle=%u reason=%d\n",
                   hub->config->role,
                   lost_handle,
                   event->disconnect.reason);

            hub->connected = false;
            hub->connecting = false;
            hub->found = false;
            hub->conn_handle = 0;

            hub->service_start = 0;
            hub->service_end = 0;
            hub->characteristic_handle = 0;
            hub->cccd_handle = 0;

            hub->gatt_ready = false;
            hub->notifications_ready = false;
            hub->battery_percent = -1;
            hub->applied_speed = 0;
            hub->motor_port0_present = false;

            /*
             * Stop the other reachable motor whenever
             * either connection is lost.
             */
            stop_all_connected_motors("BLE HUB DISCONNECTED");

            if (hub == &hub_states[HUB_LOCOMOTIVE]) {

                /*
                 * Preserve the existing locomotive recovery
                 * and zero-speed-on-reconnect behavior.
                 */
                hub_connecting = false;
                hub_connected = false;
                hub_found = false;

                lego_gatt_ready = false;
                lego_notifications_ready = false;
                hub_battery_percent = -1;

                requested_speed = 0;

                printf("HUB DISCONNECTED reason=%d\n",
                       event->disconnect.reason);

                printf("AUTO: WILL SEARCH FOR HUB AGAIN\n");
                schedule_auto_scan();
            }
            else {
                /*
                 * Reconnect at zero requested speed.
                 * Physical stop-both behavior must be
                 * implemented before movement is enabled.
                 */
                requested_speed = 0;

                printf("CABOOSE DISCONNECTED - RECONNECTING\n");

                schedule_auto_scan();
            }

            break;
        }


        case BLE_GAP_EVENT_NOTIFY_RX: {

            uint16_t notify_handle =
                event->notify_rx.conn_handle;

            sensortrain_hub_state_t *hub = NULL;

            for (int i = 0; i < SENSORTRAIN_HUB_COUNT; i++) {
                if (hub_states[i].connected &&
                    hub_states[i].conn_handle == notify_handle) {

                    hub = &hub_states[i];
                    break;
                }
            }

            if (hub == NULL) {
                printf("ERROR NOTIFICATION UNKNOWN HUB handle=%u\n",
                       notify_handle);
                break;
            }

            int len =
                OS_MBUF_PKTLEN(event->notify_rx.om);

            if (len > 128) {
                len = 128;
            }

            uint8_t data[128];

            if (os_mbuf_copydata(
                    event->notify_rx.om,
                    0,
                    len,
                    data) != 0) {

                printf("ERROR READING NOTIFICATION\n");
                break;
            }

            /*
             * Hub Property Update:
             *
             *   byte 2 = 0x01  Hub Properties
             *   byte 3 = 0x06  Battery Voltage [%]
             *   byte 4 = 0x06  Update
             *   byte 5 = 0..100 percent
             */
            if (len >= 6 &&
                data[2] == 0x01 &&
                data[3] == 0x06 &&
                data[4] == 0x06) {

                int value = (int)data[5];

                if (value >= 0 && value <= 100) {

                    hub->battery_percent = value;

                    if (hub == &hub_states[HUB_LOCOMOTIVE]) {

                        /*
                         * Retain the existing Pi motor
                         * bridge battery message format.
                         */
                        hub_battery_percent = value;

                        printf("HUB BATTERY %d%%\n", value);
                    }
                    else {

                        printf("CABOOSE BATTERY %d%%\n",
                               value);
                    }
                }
            }


            printf("LEGO RX:");

            for (int i = 0; i < len; i++) {
                printf(" %02X", data[i]);
            }

            printf("\n");

            /*
             * LEGO Hub Attached I/O message = type 0x04.
             * Header bytes:
             * [0] length
             * [1] hub id
             * [2] message type
             * [3] port
             * [4] attach event
             */
            if (len >= 5 && data[2] == 0x04) {

                /*
                 * SENSORTRAIN_V12_CALIBRATION_PREFLIGHT
                 *
                 * Port 0 attachment:
                 * Event 0 = detached
                 * Event 1 = attached
                 * Type 0x0002 = System Train Motor
                 *
                 * Unknown attachments fail closed.
                 */
                if (data[3] == 0) {

                    bool train_motor = false;

                    if (data[4] == 0x01 && len >= 7) {

                        uint16_t type =
                            data[5] |
                            ((uint16_t)data[6] << 8);

                        train_motor = (type == 0x0002);
                    }

                    /*
                     * SENSORTRAIN_V12_MOTOR_DETACH_FAILSAFE
                     *
                     * A previously attached motor has
                     * disappeared. Stop every reachable hub.
                     */
                    bool motor_was_present =
                        hub->motor_port0_present;

                    hub->motor_port0_present = train_motor;

                    printf(
                        "%s: MOTOR PORT0=%s\n",
                        hub->config->role,
                        train_motor ? "DETECTED" : "NOT_DETECTED"
                    );

                    if (motor_was_present && !train_motor) {

                        printf(
                            "%s: SAFETY MOTOR DETACH DETECTED\n",
                            hub->config->role
                        );

                        stop_all_connected_motors(
                            "MOTOR PORT0 DETACHED"
                        );
                    }
                }

                printf("ATTACHED-I/O: PORT=%u EVENT=%u",
                       data[3],
                       data[4]);

                if (data[4] == 0x01 && len >= 7) {

                    uint16_t io_type =
                        data[5] |
                        ((uint16_t)data[6] << 8);

                    printf(" TYPE=0x%04X",
                           io_type);

                    if (io_type == 0x0002) {
                        printf(" (SYSTEM TRAIN MOTOR)");
                    }
                    else if (io_type == 0x0001) {
                        printf(" (MOTOR)");
                    }
                    else if (io_type == 0x0026) {
                        printf(" (EXTERNAL TACHO MOTOR)");
                    }
                }

                printf("\n");
            }

            break;
        }


        default:
            break;
    }

    return 0;
}


static void start_ble_scan(void)
{
    if (!ble_ready) {
        printf("ERROR BLE_NOT_READY\n");
        return;
    }

    if (ble_gap_disc_active()) {
        printf("ERROR SCAN_ALREADY_ACTIVE\n");
        return;
    }

    hub_found = false;

    for (int i = 0; i < SENSORTRAIN_HUB_COUNT; i++) {
        hub_states[i].found = false;
    }

    struct ble_gap_disc_params params = {0};

    params.passive = 0;
    params.filter_duplicates = 1;
    params.filter_policy = 0;
    params.limited = 0;

    int rc = ble_gap_disc(
        own_addr_type,
        5000,
        &params,
        ble_gap_event,
        NULL
    );

    if (rc != 0) {
        printf("ERROR SCAN_START rc=%d\n", rc);
        return;
    }

    printf("SCANNING 5 SECONDS...\n");
}


static void connect_to_configured_hub(
    sensortrain_hub_id_t id)
{
    if (id < 0 || id >= SENSORTRAIN_HUB_COUNT) {
        printf("ERROR INVALID_HUB_ID\n");
        return;
    }

    sensortrain_hub_state_t *hub = &hub_states[id];

    if (!ble_ready) {
        printf("ERROR BLE_NOT_READY\n");
        return;
    }

    if (hub->connected || hub->connecting ||
        connection_in_progress()) {

        printf("%s: ALREADY CONNECTED OR CONNECTING\n",
               hub->config->role);
        return;
    }

    if (!hub->found) {
        printf("%s: NOT FOUND - SCAN FIRST\n",
               hub->config->role);
        return;
    }

    if (ble_gap_disc_active()) {
        ble_gap_disc_cancel();
    }

    printf("%s: CONNECTING...\n",
           hub->config->role);

    hub->connecting = true;

    /* Preserve legacy status only for the locomotive. */
    if (id == HUB_LOCOMOTIVE) {
        hub_connecting = true;
    }

    int rc = ble_gap_connect(
        own_addr_type,
        &hub->address,
        30000,
        NULL,
        ble_gap_event,
        hub
    );

    if (rc != 0) {

        hub->connecting = false;
        hub->found = false;

        if (id == HUB_LOCOMOTIVE) {
            hub_connecting = false;
            hub_found = false;
        }

        printf("%s: ERROR CONNECT_START rc=%d\n",
               hub->config->role, rc);

        schedule_auto_scan();
    }
}


static void ble_on_reset(int reason)
{
    ble_ready = false;
    printf("BLE RESET reason=%d\n", reason);
}


static void ble_on_sync(void)
{
    if (ble_hs_util_ensure_addr(0) != 0) {
        printf("ERROR BLE_ADDR\n");
        return;
    }

    if (ble_hs_id_infer_auto(
            0,
            &own_addr_type) != 0) {

        printf("ERROR BLE_ADDR_TYPE\n");
        return;
    }

    ble_ready = true;
    printf("BLE READY\n");
    printf("AUTO: HUB RECOVERY ENABLED\n");

    schedule_auto_scan();
}


static void ble_host_task(void *param)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}



/* ---------------------------------------------------------
 * LEGO train motor control
 * --------------------------------------------------------- */

/*
 * SENSORTRAIN_V12_PER_HUB_MOTOR_CALLBACK
 *
 * Identify the hub associated with each BLE write result.
 * A successful callback confirms the BLE write completed,
 * not that a physical motor has stopped or moved.
 */
static int motor_write_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    struct ble_gatt_attr *attr,
    void *arg)
{
    sensortrain_hub_state_t *hub =
        (sensortrain_hub_state_t *)arg;

    if (hub == &hub_states[HUB_LOCOMOTIVE] ||
        hub == &hub_states[HUB_CABOOSE]) {

        if (error->status == 0) {

            printf("%s: MOTOR COMMAND SENT conn=%u\n",
                   hub->config->role,
                   (unsigned)conn_handle);
        }
        else {

            printf("%s: ERROR MOTOR WRITE status=%d conn=%u\n",
                   hub->config->role,
                   error->status,
                   (unsigned)conn_handle);
        }
    }
    else {

        /*
         * Preserve legacy callback output for callers
         * that do not supply a hub context.
         */
        if (error->status == 0) {

            printf("MOTOR COMMAND SENT\n");
        }
        else {

            printf("ERROR MOTOR WRITE status=%d\n",
                   error->status);
        }
    }

    return 0;
}

/*
 * SENSORTRAIN_V12_PER_HUB_MOTOR_OUTPUT
 *
 * Submit a motor command to one specific LEGO hub.
 *
 * During development, only zero power is permitted.
 * Motor directions have not yet been calibrated.
 */
static bool send_motor_power_to_hub(
    sensortrain_hub_state_t *hub,
    int power)
{
    if (hub != &hub_states[HUB_LOCOMOTIVE] &&
        hub != &hub_states[HUB_CABOOSE]) {

        printf("ERROR INVALID MOTOR HUB\n");
        return false;
    }

    if (power < -100 || power > 100) {
        printf("ERROR POWER RANGE\n");
        return false;
    }

    /*
     * Independent development safety interlock.
     * Never permit movement at this stage.
     */
    if (power != 0) {
        printf("%s: ERROR DUAL_MOTOR_CONTROL_NOT_READY\n",
               hub->config->role);
        return false;
    }

    if (!hub->connected ||
        !hub->gatt_ready ||
        hub->characteristic_handle == 0) {

        printf("%s: ERROR HUB NOT READY\n",
               hub->config->role);
        return false;
    }

    uint8_t command[8] = {
        0x08, 0x00, 0x81, 0x00,
        0x11, 0x51, 0x00,
        (uint8_t)(int8_t)power
    };

    int rc = ble_gattc_write_flat(
        hub->conn_handle,
        hub->characteristic_handle,
        command,
        sizeof(command),
        motor_write_cb,
        hub
    );

    if (rc != 0) {
        printf("%s: ERROR MOTOR WRITE START rc=%d\n",
               hub->config->role, rc);
        return false;
    }

    return true;
}


/*
 * SENSORTRAIN_V12_MOTION_INTERLOCK
 *
 * Legacy single-hub power output removed.
 * send_motor_power_to_hub() rejects nonzero power.
 * The coordinated speed planner remains locked.
 */


/*
 * SENSORTRAIN_V12_COORDINATED_STOP
 *
 * Send zero power to every connected, GATT-ready hub.
 * Submission is not proof of physical motor stoppage.
 */
static void stop_all_connected_motors(const char *reason)
{
    requested_speed = 0;

    printf("SAFETY STOP: %s\n", reason);

    for (int i = 0; i < SENSORTRAIN_HUB_COUNT; i++) {

        sensortrain_hub_state_t *hub = &hub_states[i];

        if (!hub->connected ||
            !hub->gatt_ready ||
            hub->characteristic_handle == 0) {

            printf("%s: STOP UNAVAILABLE - HUB NOT READY\n",
                   hub->config->role);
            continue;
        }

        if (send_motor_power_to_hub(hub, 0)) {

            printf("%s: STOP COMMAND SUBMITTED\n",
                   hub->config->role);
        }
        else {

            printf("%s: ERROR STOP SUBMISSION\n",
                   hub->config->role);
        }
    }
}


/*
 * SENSORTRAIN_V12_DUAL_READINESS
 *
 * Check both BLE links and calibrated motor directions.
 * This is NOT permission to enable movement.
 */
static bool dual_hubs_configured_and_ready(void)
{
    for (int i = 0; i < SENSORTRAIN_HUB_COUNT; i++) {

        const sensortrain_hub_state_t *hub = &hub_states[i];

        if (!hub->connected ||
            !hub->gatt_ready ||
            !hub->notifications_ready ||
            hub->characteristic_handle == 0) {
            return false;
        }

        if (hub->config->forward_sign != 1 &&
            hub->config->forward_sign != -1) {
            return false;
        }
    }

    return true;
}


/*
 * SENSORTRAIN_V12_DUAL_SPEED_PLANNER
 *
 * Development mode: calculate intended motor directions,
 * but NEVER submit nonzero power to either hub.
 */
static bool dispatch_dual_motor_speed(int speed)
{
    if (speed < -100 || speed > 100) {
        printf("ERROR SPEED_RANGE\n");
        return false;
    }

    if (speed == 0) {
        stop_all_connected_motors("SPEED ZERO");
        printf("SPEED 0\n");
        return true;
    }

    if (!dual_hubs_configured_and_ready()) {

        printf("ERROR DUAL_MOTOR_PAIR_NOT_READY\n");

    } else {

        int locomotive_power =
            speed *
            hub_states[HUB_LOCOMOTIVE].config->forward_sign;

        int caboose_power =
            speed *
            hub_states[HUB_CABOOSE].config->forward_sign;

        printf(
            "DUALPLAN LOCOMOTIVE=%d CABOOSE=%d "
            "MOTION=LOCKED\n",
            locomotive_power,
            caboose_power
        );
    }

    /*
     * HARD DEVELOPMENT INTERLOCK.
     *
     * Planning is not permission to move.
     * Do not call either motor-output function here.
     */
    printf("ERROR DUAL_MOTOR_CONTROL_NOT_READY\n");

    return false;
}


static void process_command(const char *line)
{
    if (strcmp(line, "PING") == 0) {

        last_heartbeat_us = esp_timer_get_time();
        printf("PONG\n");
    }

    else if (strcmp(line, "STATUS") == 0) {

        printf(
            "STATUS SPEED=%d BLE=%s HUB=%s GATT=%s NOTIFY=%s BATTERY=%d\n",
            requested_speed,
            ble_ready ? "READY" : "NOT_READY",
            hub_connected ? "CONNECTED" : "DISCONNECTED",
            lego_gatt_ready ? "READY" : "NOT_READY",
            lego_notifications_ready ?
                "READY" : "NOT_READY",
            hub_battery_percent
        );
    }

    else if (strcmp(line, "DUALSTATUS") == 0) {

        for (int i = 0; i < SENSORTRAIN_HUB_COUNT; i++) {

            const sensortrain_hub_state_t *hub =
                &hub_states[i];

            printf(
                "DUALSTATUS %s CONNECTED=%s GATT=%s "
                "NOTIFY=%s SIGN=%d BATTERY=%d\n",
                hub->config->role,
                hub->connected ? "YES" : "NO",
                hub->gatt_ready ? "READY" : "NOT_READY",
                hub->notifications_ready ? "READY" : "NOT_READY",
                (int)hub->config->forward_sign,
                hub->battery_percent
            );
        }

        printf(
            "DUALSTATUS PAIR_READY=%s MOTION=LOCKED\n",
            dual_hubs_configured_and_ready() ? "YES" : "NO"
        );
    }

    else if (strcmp(line, "CALSTATUS") == 0) {

        bool precheck = (requested_speed == 0);

        for (int i = 0; i < SENSORTRAIN_HUB_COUNT; i++) {

            const sensortrain_hub_state_t *hub =
                &hub_states[i];

            bool link_ready =
                hub->connected &&
                hub->gatt_ready &&
                hub->notifications_ready &&
                hub->characteristic_handle != 0;

            bool motor_ready =
                link_ready && hub->motor_port0_present;

            if (!motor_ready) {
                precheck = false;
            }

            printf(
                "CALSTATUS %s LINK=%s MOTOR0=%s BATTERY=%d\n",
                hub->config->role,
                link_ready ? "READY" : "NOT_READY",
                hub->motor_port0_present ? "YES" : "NO",
                hub->battery_percent
            );
        }

        printf(
            "CALSTATUS PRECHECK=%s MOTION=LOCKED\n",
            precheck ? "YES" : "NO"
        );
    }

    else if (strcmp(line, "SCAN") == 0) {

        start_ble_scan();
    }

    else if (strcmp(line, "CONNECT") == 0) {

        connect_to_configured_hub(HUB_LOCOMOTIVE);
    }

    else if (strcmp(line, "MOTORTEST") == 0) {

        /*
         * Legacy single-hub test permanently disabled.
         * Future calibration uses a separate controlled path.
         */
        printf("ERROR MOTORTEST_DISABLED\n");
    }

    else if (strcmp(line, "STOP") == 0) {

        stop_all_connected_motors("HOST STOP");
        printf("STOPPED\n");
    }

    else if (strncmp(line, "SPEED ", 6) == 0) {

        char *endptr = NULL;
        long value = strtol(line + 6, &endptr, 10);

        if (endptr == line + 6 || *endptr != '\0') {

            printf("ERROR INVALID_SPEED\n");

        } else if (value < -100 || value > 100) {

            printf("ERROR SPEED_RANGE\n");

        } else {

            dispatch_dual_motor_speed((int)value);
        }
    }

    else if (line[0] != '\0') {

        printf("ERROR UNKNOWN_COMMAND: %s\n",
               line);
    }
}


void app_main(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);

    initialize_hub_states();

    esp_err_t ret = nvs_flash_init();

    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {

        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }

    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(nimble_port_init());

    ble_hs_cfg.reset_cb = ble_on_reset;
    ble_hs_cfg.sync_cb = ble_on_sync;

    nimble_port_freertos_init(
        ble_host_task
    );

    printf("\n");
    printf("SensorTrain Motor Controller\n");
    printf("READY\n");
    printf("> ");

    char line[128];
    size_t pos = 0;

    while (1) {

        /*
         * Motor failsafe:
         * If motor is running and the Pi stops sending
         * heartbeats for 1 second, stop immediately.
         */
        if (requested_speed != 0) {

            int64_t now = esp_timer_get_time();

            if ((now - last_heartbeat_us) >
                    HEARTBEAT_TIMEOUT_US) {

                printf("\nFAILSAFE: HEARTBEAT LOST - STOPPING MOTOR\n");

                stop_all_connected_motors(
                    "HEARTBEAT TIMEOUT");
            }
        }

        int c = getchar();

        if (c == EOF) {

            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }

        if (c == '\r' || c == '\n') {

            if (pos > 0) {

                line[pos] = '\0';

                printf("\n");

                process_command(line);

                pos = 0;
            }

            printf("> ");
            continue;
        }

        if (c == '\b' || c == 127) {

            if (pos > 0) {

                pos--;
                printf("\b \b");
            }

            continue;
        }

        if (isprint(c) &&
            pos < sizeof(line) - 1) {

            line[pos++] = (char)c;

            putchar(c);
        }
    }
}
