/*
 * The simulated battery pack and the BMS state machine.
 *
 * Plain C with no Zephyr calls, so the logic can be read (and tested) on its
 * own. main.c is the Zephyr glue: threads, queue, timer, CAN, watchdog.
 */
#ifndef PACK_H
#define PACK_H

#include <stdbool.h>
#include <stdint.h>

#define PACK_CELLS 8
#define PACK_TEMPS 4

/* Fault flag bits, as in the BmsStatus.FaultFlags and BmsFault signals. */
#define FAULT_OVER_VOLTAGE  0x01
#define FAULT_UNDER_VOLTAGE 0x02
#define FAULT_OVER_TEMP     0x04
#define FAULT_OVER_CURRENT  0x08

/* Limits. A fault is raised above the trip value and may be reset only
 * once the value is back below the clear value (hysteresis). */
#define CELL_OV_TRIP_MV   4250
#define CELL_OV_CLEAR_MV  4200
#define CELL_UV_TRIP_MV   3000
#define CELL_UV_CLEAR_MV  3100
#define TEMP_OT_TRIP_C    60.0f
#define TEMP_OT_CLEAR_C   50.0f
#define CURRENT_OC_TRIP_A 100.0f

enum bms_state { BMS_INIT = 0, BMS_STANDBY = 1, BMS_ACTIVE = 2, BMS_FAULT = 3 };

/* BmsCommand.Cmd values */
enum bms_cmd {
	CMD_CONTACTOR_CLOSE = 0x01,
	CMD_CONTACTOR_OPEN = 0x02,
	CMD_BALANCING_ON = 0x03,
	CMD_BALANCING_OFF = 0x04,
	CMD_FAULT_RESET = 0x05,
	CMD_SIM_NORMAL = 0x10,
	CMD_SIM_OVER_TEMP = 0x11,
	CMD_SIM_OVER_VOLTAGE = 0x12,
	CMD_SIM_HANG = 0x7E, /* handled in main.c: stalls the BMS thread */
};

/* BmsCommandAck.Result values */
enum cmd_result { RESULT_OK = 0, RESULT_REJECTED = 1, RESULT_UNKNOWN = 2 };

enum scenario { SCEN_NORMAL, SCEN_OVER_TEMP, SCEN_OVER_VOLTAGE };

struct pack {
	/* physics */
	float soc;                       /* state of charge, 0..1 */
	float current_a;                 /* positive = discharging */
	float cell_offset_v[PACK_CELLS]; /* each cell's deviation from the average */
	float temp_c[PACK_TEMPS];
	enum scenario scenario;

	/* outputs of the last step */
	uint16_t cell_mv[PACK_CELLS];
	uint8_t balancing_mask;

	/* BMS */
	enum bms_state state;
	bool contactor;
	bool balancing;
	uint8_t faults;    /* latched fault flags */
	uint8_t new_fault; /* raised in the last step (one flag), else 0 */
	uint8_t fault_index;
	int16_t fault_value;
};

void pack_init(struct pack *p);

/* Advance the simulation by dt seconds, then check limits. */
void pack_step(struct pack *p, float dt);

/* Apply a command from the edge controller. */
enum cmd_result pack_command(struct pack *p, uint8_t cmd);

/* Derived values for the CAN frames */
uint32_t pack_voltage_mv(const struct pack *p);
void pack_cell_min_max(const struct pack *p, int *min_i, int *max_i);
int pack_temp_max(const struct pack *p);

const char *pack_state_name(enum bms_state s);

#endif
