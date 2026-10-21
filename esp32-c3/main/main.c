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

static int requested_speed = 0;

static int64_t last_heartbeat_us = 0;

#define HEARTBEAT_TIMEOUT_US 1000000


static bool ble_ready = false;
static uint8_t own_addr_type;

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

static int battery_request_write_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    struct ble_gatt_attr *attr,
    void *arg)
{
    if (error->status != 0) {
        printf(
            "ERROR BATTERY REQUEST WRITE status=%d\n",
            error->status
        );
    }

    return 0;
}


static int battery_enable_write_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    struct ble_gatt_attr *attr,
    void *arg)
{
    if (error->status != 0) {
        printf(
            "ERROR BATTERY UPDATE ENABLE status=%d\n",
            error->status
        );

        return 0;
    }

    printf("HUB BATTERY UPDATES ENABLED\n");

    /*
     * Immediately request the current value as well.  This
     * avoids waiting for the battery percentage to change
     * before the dashboard gets its first reading.
     */
    uint8_t request_battery[] = {
        0x05, 0x00, 0x01, 0x06, 0x05
    };

    int rc = ble_gattc_write_flat(
        conn_handle,
        lego_characteristic_handle,
        request_battery,
        sizeof(request_battery),
        battery_request_write_cb,
        NULL
    );

    if (rc != 0) {
        printf(
            "ERROR BATTERY REQUEST START rc=%d\n",
            rc
        );
    }

    return 0;
}


static void enable_hub_battery_updates(void)
{
    if (!hub_connected ||
        lego_characteristic_handle == 0) {

        printf("ERROR BATTERY MONITOR NOT READY\n");
        return;
    }

    uint8_t enable_updates[] = {
        0x05, 0x00, 0x01, 0x06, 0x02
    };

    int rc = ble_gattc_write_flat(
        hub_conn_handle,
        lego_characteristic_handle,
        enable_updates,
        sizeof(enable_updates),
        battery_enable_write_cb,
        NULL
    );

    if (rc != 0) {
        printf(
            "ERROR BATTERY UPDATE ENABLE START rc=%d\n",
            rc
        );
    }
}


static int notification_enable_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    struct ble_gatt_attr *attr,
    void *arg)
{
    if (error->status == 0) {
        lego_notifications_ready = true;

        printf("LEGO NOTIFICATIONS ENABLED\n");

        enable_hub_battery_updates();
        printf("UNPLUG AND REPLUG THE TRAIN MOTOR NOW\n");
    }
    else {
        printf("ERROR ENABLING NOTIFICATIONS status=%d\n",
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
    if (error->status == 0 && dsc != NULL) {

        if (ble_uuid_cmp(
                &dsc->uuid.u,
                BLE_UUID16_DECLARE(
                    BLE_GATT_DSC_CLT_CFG_UUID16)) == 0) {

            lego_cccd_handle = dsc->handle;

            printf("LEGO CCCD FOUND handle=%u\n",
                   lego_cccd_handle);
        }

        return 0;
    }

    if (error->status == BLE_HS_EDONE) {

        if (lego_cccd_handle == 0) {
            printf("ERROR LEGO_CCCD_NOT_FOUND\n");
            return 0;
        }

        uint8_t enable_notifications[2] = { 0x01, 0x00 };

        int rc = ble_gattc_write_flat(
            conn_handle,
            lego_cccd_handle,
            enable_notifications,
            sizeof(enable_notifications),
            notification_enable_cb,
            NULL
        );

        if (rc != 0) {
            printf("ERROR CCCD_WRITE rc=%d\n", rc);
        }

        return 0;
    }

    printf("ERROR DESCRIPTOR_DISCOVERY status=%d\n",
           error->status);

    return 0;
}


static int characteristic_discovery_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    const struct ble_gatt_chr *chr,
    void *arg)
{
    if (error->status == 0 && chr != NULL) {

        lego_characteristic_handle = chr->val_handle;

        printf("LEGO CHARACTERISTIC FOUND handle=%u\n",
               lego_characteristic_handle);

        return 0;
    }

    if (error->status == BLE_HS_EDONE) {

        if (lego_characteristic_handle == 0) {
            printf("ERROR LEGO_CHARACTERISTIC_NOT_FOUND\n");
            return 0;
        }

        lego_gatt_ready = true;

        printf("LEGO GATT READY\n");
        printf("SEARCHING FOR NOTIFICATION DESCRIPTOR...\n");

        int rc = ble_gattc_disc_all_dscs(
            conn_handle,
            lego_characteristic_handle,
            lego_service_end,
            descriptor_discovery_cb,
            NULL
        );

        if (rc != 0) {
            printf("ERROR DESCRIPTOR_DISCOVERY_START rc=%d\n",
                   rc);
        }

        return 0;
    }

    printf("ERROR CHARACTERISTIC_DISCOVERY status=%d\n",
           error->status);

    return 0;
}


static int service_discovery_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    const struct ble_gatt_svc *service,
    void *arg)
{
    if (error->status == 0 && service != NULL) {

        lego_service_start = service->start_handle;
        lego_service_end = service->end_handle;

        printf("LEGO SERVICE FOUND start=%u end=%u\n",
               lego_service_start,
               lego_service_end);

        return 0;
    }

    if (error->status == BLE_HS_EDONE) {

        if (lego_service_start == 0) {
            printf("ERROR LEGO_SERVICE_NOT_FOUND\n");
            return 0;
        }

        printf("SEARCHING FOR LEGO CHARACTERISTIC...\n");

        int rc = ble_gattc_disc_chrs_by_uuid(
            conn_handle,
            lego_service_start,
            lego_service_end,
            &lego_characteristic_uuid.u,
            characteristic_discovery_cb,
            NULL
        );

        if (rc != 0) {
            printf("ERROR CHARACTERISTIC_DISCOVERY_START rc=%d\n",
                   rc);
        }

        return 0;
    }

    printf("ERROR SERVICE_DISCOVERY status=%d\n",
           error->status);

    return 0;
}


static void start_lego_gatt_discovery(void)
{
    lego_service_start = 0;
    lego_service_end = 0;
    lego_characteristic_handle = 0;
    lego_cccd_handle = 0;
    lego_gatt_ready = false;
    lego_notifications_ready = false;

    printf("SEARCHING FOR LEGO SERVICE...\n");

    int rc = ble_gattc_disc_svc_by_uuid(
        hub_conn_handle,
        &lego_service_uuid.u,
        service_discovery_cb,
        NULL
    );

    if (rc != 0) {
        printf("ERROR SERVICE_DISCOVERY_START rc=%d\n", rc);
    }
}



/* ---------------------------------------------------------
 * AUTO-RECOVERY SUPPORT
 * --------------------------------------------------------- */

static void start_ble_scan(void);
static void connect_to_hub(void);

static bool auto_scan_task_pending = false;

static void auto_scan_task(void *arg)
{
    /*
     * Wait briefly before rescanning.  This gives NimBLE time
     * to completely finish a disconnect or failed connection.
     */
    vTaskDelay(pdMS_TO_TICKS(1000));

    auto_scan_task_pending = false;

    if (ble_ready &&
        !hub_connected &&
        !hub_connecting &&
        !ble_gap_disc_active()) {

        printf("AUTO: SEARCHING FOR HUB NO.4...\n");
        start_ble_scan();
    }

    vTaskDelete(NULL);
}

static void schedule_auto_scan(void)
{
    if (auto_scan_task_pending ||
        hub_connected ||
        hub_connecting) {

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

                static const uint8_t target_hub_addr[6] = {
                    0xA8, 0xD2, 0x01,
                    0xC0, 0x9A, 0x9C
                };

                bool name_matches =
                    fields.name_len == strlen("HUB NO.4") &&
                    memcmp(fields.name,
                           "HUB NO.4",
                           fields.name_len) == 0;

                bool address_matches =
                    memcmp(event->disc.addr.val,
                           target_hub_addr,
                           sizeof(target_hub_addr)) == 0;

                if (name_matches && address_matches) {

                    memcpy(&hub_addr,
                           &event->disc.addr,
                           sizeof(hub_addr));

                    hub_found = true;

                    printf("  <-- SENSORTRAIN HUB");

                    /*
                     * We found the one specific SensorTrain hub.
                     * Stop scanning and connect automatically.
                     */
                    if (ble_gap_disc_active()) {
                        ble_gap_disc_cancel();
                    }

                    connect_to_hub();
                }
                else if (name_matches) {

                    printf("  <-- HUB NO.4 IGNORED");
                }
            }

            printf("\n");
            break;
        }


        case BLE_GAP_EVENT_DISC_COMPLETE:

            printf("SCAN COMPLETE\n");

            if (!hub_connected &&
                !hub_connecting &&
                !hub_found) {

                schedule_auto_scan();
            }

            break;


        case BLE_GAP_EVENT_CONNECT:

            if (event->connect.status == 0) {

                hub_connecting = false;
                hub_connected = true;
                hub_conn_handle =
                    event->connect.conn_handle;

                printf("HUB CONNECTED handle=%d\n",
                       hub_conn_handle);

                start_lego_gatt_discovery();
            }
            else {

                hub_connecting = false;
                hub_connected = false;
                hub_found = false;

                printf("HUB CONNECTION FAILED status=%d\n",
                       event->connect.status);

                schedule_auto_scan();
            }

            break;


        case BLE_GAP_EVENT_DISCONNECT:

            hub_connecting = false;
            hub_connected = false;
            hub_found = false;

            lego_gatt_ready = false;
            lego_notifications_ready = false;

            hub_battery_percent = -1;

            /*
             * Never resume the previous motor speed after
             * reconnecting.
             */
            requested_speed = 0;

            printf("HUB DISCONNECTED reason=%d\n",
                   event->disconnect.reason);

            printf("AUTO: WILL SEARCH FOR HUB AGAIN\n");

            schedule_auto_scan();

            break;


        case BLE_GAP_EVENT_NOTIFY_RX: {

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
                    hub_battery_percent = value;

                    printf(
                        "HUB BATTERY %d%%\n",
                        hub_battery_percent
                    );
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


static void connect_to_hub(void)
{
    if (!ble_ready) {
        printf("ERROR BLE_NOT_READY\n");
        return;
    }

    if (hub_connected) {
        printf("HUB ALREADY CONNECTED\n");
        return;
    }

    if (!hub_found) {
        printf("ERROR HUB_NOT_FOUND - RUN SCAN FIRST\n");
        return;
    }

    if (ble_gap_disc_active()) {
        ble_gap_disc_cancel();
    }

    printf("CONNECTING TO HUB...\n");

    hub_connecting = true;

    int rc = ble_gap_connect(
        own_addr_type,
        &hub_addr,
        30000,
        NULL,
        ble_gap_event,
        NULL
    );

    if (rc != 0) {

        hub_connecting = false;
        hub_found = false;

        printf("ERROR CONNECT_START rc=%d\n", rc);

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

static int motor_write_cb(
    uint16_t conn_handle,
    const struct ble_gatt_error *error,
    struct ble_gatt_attr *attr,
    void *arg)
{
    if (error->status == 0) {
        printf("MOTOR COMMAND SENT\n");
    } else {
        printf("ERROR MOTOR WRITE status=%d\n",
               error->status);
    }

    return 0;
}

static bool lego_set_power(int power)
{
    if (!hub_connected || !lego_gatt_ready) {
        printf("ERROR HUB NOT READY\n");
        return false;
    }

    if (power < -100 || power > 100) {
        printf("ERROR POWER RANGE\n");
        return false;
    }

    /*
     * LEGO Port Output Command:
     *
     * 08 = message length
     * 00 = hub ID
     * 81 = Port Output Command
     * 00 = motor port 0
     * 11 = execute immediately + feedback
     * 51 = WriteDirectModeData
     * 00 = motor power mode
     * XX = power (-100..100)
     */
    uint8_t command[8] = {
        0x08,
        0x00,
        0x81,
        0x00,
        0x11,
        0x51,
        0x00,
        (uint8_t)(int8_t)power
    };

    int rc = ble_gattc_write_flat(
        hub_conn_handle,
        lego_characteristic_handle,
        command,
        sizeof(command),
        motor_write_cb,
        NULL
    );

    if (rc != 0) {
        printf("ERROR MOTOR WRITE START rc=%d\n", rc);
        return false;
    }

    return true;
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

    else if (strcmp(line, "SCAN") == 0) {

        start_ble_scan();
    }

    else if (strcmp(line, "CONNECT") == 0) {

        connect_to_hub();
    }

    else if (strcmp(line, "MOTORTEST") == 0) {

        printf("MOTOR TEST: 20%% FOR 1 SECOND\n");

        if (lego_set_power(20)) {

            vTaskDelay(pdMS_TO_TICKS(1000));

            lego_set_power(0);

            requested_speed = 0;

            printf("MOTOR TEST COMPLETE\n");
        }
    }

    else if (strcmp(line, "STOP") == 0) {

        if (lego_set_power(0)) {
            requested_speed = 0;
            printf("STOPPED\n");
        }
    }

    else if (strncmp(line, "SPEED ", 6) == 0) {

        int speed = atoi(line + 6);

        if (speed < -100 || speed > 100) {

            printf("ERROR SPEED_RANGE\n");
        }
        else {

            if (lego_set_power(speed)) {
                requested_speed = speed;
                last_heartbeat_us = esp_timer_get_time();

                printf("SPEED %d\n",
                       requested_speed);
            }
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
        if (requested_speed != 0 && hub_connected) {

            int64_t now = esp_timer_get_time();

            if ((now - last_heartbeat_us) >
                    HEARTBEAT_TIMEOUT_US) {

                printf("\nFAILSAFE: HEARTBEAT LOST - STOPPING MOTOR\n");

                lego_set_power(0);
                requested_speed = 0;
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
