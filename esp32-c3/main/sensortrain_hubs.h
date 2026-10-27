#ifndef SENSORTRAIN_HUBS_H
#define SENSORTRAIN_HUBS_H

#include <stdint.h>

#define SENSORTRAIN_HUB_COUNT 2

typedef enum {
    HUB_LOCOMOTIVE = 0,
    HUB_CABOOSE = 1
} sensortrain_hub_id_t;

typedef struct {
    const char *role;
    const char *name;
    uint8_t address[6];
    int8_t forward_sign;
} sensortrain_hub_config_t;

/*
 * Addresses are in NimBLE byte order.
 *
 * forward_sign:
 *   0 = not calibrated
 *  +1 = normal
 *  -1 = reversed
 */
static const sensortrain_hub_config_t
sensortrain_hub_configs[SENSORTRAIN_HUB_COUNT] = {
    {
        "LOCOMOTIVE",
        "ST-LOCOMOTIVE",
        {0xA8, 0xD2, 0x01, 0xC0, 0x9A, 0x9C},
        0
    },
    {
        "CABOOSE",
        "ST-CABOOSE",
        {0xDD, 0xAB, 0xCD, 0x2B, 0x84, 0x90},
        0
    }
};

#endif
