/*
 * A deliberately simple pack model: good enough to make the numbers on the
 * bus move in believable ways, nothing more.
 *
 * - 8 cells in series, 5 Ah. Open-circuit voltage is linear in SOC
 *   (3.0 V empty, 4.1 V full), minus an I*R drop under load.
 * - With the contactor closed the pack is discharged at a constant 18.5 A
 *   (SOC falls ~0.1 %/s, so it visibly moves during a demo).
 * - Temperatures settle towards ambient + I^2 heating with a first-order lag.
 * - Cells start slightly unbalanced; balancing bleeds the high ones.
 * - The Sim* commands inject faults: a hot sensor or a cell drifting high.
 */
#include "pack.h"

#include <math.h>
#include <stddef.h>

#define CAPACITY_AH      5.0f
#define LOAD_CURRENT_A   18.5f
#define CELL_R_OHM       0.005f
#define AMBIENT_C        25.0f
#define TEMP_TAU_S       20.0f
#define HOT_SENSOR       1     /* sensor index heated by SimOverTemp */
#define HOT_TARGET_C     85.0f
#define HOT_TAU_S        8.0f
#define DRIFT_CELL       2     /* cell index pushed up by SimOverVoltage */
#define DRIFT_V_PER_S    0.025f
#define BLEED_V_PER_S    0.001f
#define BALANCE_WINDOW_V 0.005f

static const float initial_offset_v[PACK_CELLS] = {
	0.000f, 0.012f, -0.008f, 0.005f, 0.020f, -0.003f, 0.002f, -0.010f,
};

void pack_init(struct pack *p)
{
	*p = (struct pack){0};
	p->soc = 0.80f;
	for (int i = 0; i < PACK_CELLS; i++) {
		p->cell_offset_v[i] = initial_offset_v[i];
	}
	for (int i = 0; i < PACK_TEMPS; i++) {
		p->temp_c[i] = AMBIENT_C + 0.5f * i;
	}
	p->scenario = SCEN_NORMAL;
	p->state = BMS_INIT;
	pack_step(p, 0.0f);
	p->state = BMS_STANDBY; /* self-test "passed" */
}

static float approach(float value, float target, float tau_s, float dt)
{
	return value + (target - value) * (1.0f - expf(-dt / tau_s));
}

static void simulate(struct pack *p, float dt)
{
	p->current_a = p->contactor ? LOAD_CURRENT_A : 0.0f;

	p->soc -= p->current_a * dt / (3600.0f * CAPACITY_AH);
	if (p->soc < 0.0f) {
		p->soc = 0.0f;
	}

	/* Fault injection, and the way back when the scenario is cleared. */
	float *drift = &p->cell_offset_v[DRIFT_CELL];
	if (p->scenario == SCEN_OVER_VOLTAGE) {
		*drift += DRIFT_V_PER_S * dt;
	} else if (*drift > initial_offset_v[DRIFT_CELL]) {
		*drift -= DRIFT_V_PER_S * dt;
		if (*drift < initial_offset_v[DRIFT_CELL]) {
			*drift = initial_offset_v[DRIFT_CELL];
		}
	}

	float ocv = 3.0f + 1.1f * p->soc;
	float load_drop = p->current_a * CELL_R_OHM;
	for (int i = 0; i < PACK_CELLS; i++) {
		float v = ocv + p->cell_offset_v[i] - load_drop;
		p->cell_mv[i] = (uint16_t)lroundf(v * 1000.0f);
	}

	float heat_target = AMBIENT_C + 0.02f * p->current_a * p->current_a;
	for (int i = 0; i < PACK_TEMPS; i++) {
		bool hot = p->scenario == SCEN_OVER_TEMP && i == HOT_SENSOR;
		float target = hot ? HOT_TARGET_C : heat_target + 0.5f * i;
		p->temp_c[i] = approach(p->temp_c[i], target, hot ? HOT_TAU_S : TEMP_TAU_S, dt);
	}

	/* Passive balancing: bleed every cell more than 5 mV above the lowest. */
	p->balancing_mask = 0;
	if (p->balancing) {
		int lo, hi;
		pack_cell_min_max(p, &lo, &hi);
		for (int i = 0; i < PACK_CELLS; i++) {
			if (p->cell_mv[i] > p->cell_mv[lo] + BALANCE_WINDOW_V * 1000.0f) {
				p->cell_offset_v[i] -= BLEED_V_PER_S * dt;
				p->balancing_mask |= 1u << i;
			}
		}
	}
}

static void raise_fault(struct pack *p, uint8_t flag, int index, int value)
{
	if (p->faults & flag) {
		return; /* already latched */
	}
	p->faults |= flag;
	if (!p->new_fault) {
		p->new_fault = flag;
		p->fault_index = (uint8_t)index;
		p->fault_value = (int16_t)value;
	}
}

static void check_limits(struct pack *p)
{
	int lo, hi;
	pack_cell_min_max(p, &lo, &hi);
	int t = pack_temp_max(p);

	p->new_fault = 0;
	if (p->cell_mv[hi] > CELL_OV_TRIP_MV) {
		raise_fault(p, FAULT_OVER_VOLTAGE, hi, p->cell_mv[hi]);
	}
	if (p->cell_mv[lo] < CELL_UV_TRIP_MV) {
		raise_fault(p, FAULT_UNDER_VOLTAGE, lo, p->cell_mv[lo]);
	}
	if (p->temp_c[t] > TEMP_OT_TRIP_C) {
		raise_fault(p, FAULT_OVER_TEMP, t, (int)lroundf(p->temp_c[t] * 10.0f));
	}
	if (fabsf(p->current_a) > CURRENT_OC_TRIP_A) {
		raise_fault(p, FAULT_OVER_CURRENT, 0, (int)lroundf(p->current_a * 10.0f));
	}

	/* Any latched fault: open the contactor and stay in FAULT until reset. */
	if (p->faults && p->state != BMS_INIT) {
		p->state = BMS_FAULT;
		p->contactor = false;
		p->current_a = 0.0f;
	}
}

void pack_step(struct pack *p, float dt)
{
	simulate(p, dt);
	check_limits(p);
}

static bool conditions_clear(const struct pack *p)
{
	int lo, hi;
	pack_cell_min_max(p, &lo, &hi);
	return p->cell_mv[hi] < CELL_OV_CLEAR_MV && p->cell_mv[lo] > CELL_UV_CLEAR_MV &&
	       p->temp_c[pack_temp_max(p)] < TEMP_OT_CLEAR_C;
}

enum cmd_result pack_command(struct pack *p, uint8_t cmd)
{
	switch (cmd) {
	case CMD_CONTACTOR_CLOSE:
		if (p->state != BMS_STANDBY) {
			return p->state == BMS_ACTIVE ? RESULT_OK : RESULT_REJECTED;
		}
		p->contactor = true;
		p->state = BMS_ACTIVE;
		return RESULT_OK;
	case CMD_CONTACTOR_OPEN:
		p->contactor = false;
		if (p->state == BMS_ACTIVE) {
			p->state = BMS_STANDBY;
		}
		return RESULT_OK;
	case CMD_BALANCING_ON:
		if (p->state == BMS_FAULT) {
			return RESULT_REJECTED;
		}
		p->balancing = true;
		return RESULT_OK;
	case CMD_BALANCING_OFF:
		p->balancing = false;
		return RESULT_OK;
	case CMD_FAULT_RESET:
		if (p->state != BMS_FAULT) {
			return RESULT_OK;
		}
		if (!conditions_clear(p)) {
			return RESULT_REJECTED;
		}
		p->faults = 0;
		p->state = BMS_STANDBY;
		return RESULT_OK;
	case CMD_SIM_NORMAL:
		p->scenario = SCEN_NORMAL;
		return RESULT_OK;
	case CMD_SIM_OVER_TEMP:
		p->scenario = SCEN_OVER_TEMP;
		return RESULT_OK;
	case CMD_SIM_OVER_VOLTAGE:
		p->scenario = SCEN_OVER_VOLTAGE;
		return RESULT_OK;
	default:
		return RESULT_UNKNOWN;
	}
}

uint32_t pack_voltage_mv(const struct pack *p)
{
	uint32_t sum = 0;
	for (int i = 0; i < PACK_CELLS; i++) {
		sum += p->cell_mv[i];
	}
	return sum;
}

void pack_cell_min_max(const struct pack *p, int *min_i, int *max_i)
{
	*min_i = 0;
	*max_i = 0;
	for (int i = 1; i < PACK_CELLS; i++) {
		if (p->cell_mv[i] < p->cell_mv[*min_i]) {
			*min_i = i;
		}
		if (p->cell_mv[i] > p->cell_mv[*max_i]) {
			*max_i = i;
		}
	}
}

int pack_temp_max(const struct pack *p)
{
	int m = 0;
	for (int i = 1; i < PACK_TEMPS; i++) {
		if (p->temp_c[i] > p->temp_c[m]) {
			m = i;
		}
	}
	return m;
}

const char *pack_state_name(enum bms_state s)
{
	static const char *const names[] = {"INIT", "STANDBY", "ACTIVE", "FAULT"};
	return (unsigned)s < 4 ? names[s] : "?";
}
