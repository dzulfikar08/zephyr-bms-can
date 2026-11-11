/*
 * zephyr-bms: a fake battery management system that talks CAN.
 *
 * Who does what:
 *
 *   tick_timer (k_timer, 100 ms) ──EVT_TICK──┐
 *   CAN RX filter callbacks (ISR) ─EVT_CMD───┼─► evt_q (k_msgq) ─► bms thread
 *                                 └EVT_EDGE──┘                       │ pack model,
 *                                                                    │ state machine
 *                                                                    ▼
 *                                         tx_q (k_msgq of can_frame) ─► can_tx thread ─► can_send()
 *
 * Both threads have a task watchdog channel; the task watchdog in turn feeds
 * the STM32 independent watchdog (IWDG), so a stuck thread resets the chip.
 *
 * Frame layouts are in dbc/zephyr-bms.dbc.
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/can.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/task_wdt/task_wdt.h>

#include "pack.h"

LOG_MODULE_REGISTER(bms, LOG_LEVEL_INF);

/* CAN identifiers (11-bit). 0x100/0x101/0x321 belong to edge-linux-can. */
#define ID_STATUS       0x410
#define ID_CELL_SUMMARY 0x411
#define ID_TEMPERATURES 0x412
#define ID_CELL_VOLTS   0x413
#define ID_HEARTBEAT    0x41F
#define ID_COMMAND      0x420
#define ID_COMMAND_ACK  0x421
#define ID_FAULT        0x430
#define ID_EDGE_HB      0x101 /* edge-linux-can can-demo heartbeat on can0 */

#define TICK_MS          100
#define EDGE_TIMEOUT_MS  3000
#define BMS_WDT_MS       1000
#define TX_WDT_MS        2000

enum evt_type { EVT_TICK, EVT_CMD, EVT_EDGE };

struct bms_evt {
	uint8_t type;
	uint8_t cmd;
	uint8_t token;
};

K_MSGQ_DEFINE(evt_q, sizeof(struct bms_evt), 32, 4);
K_MSGQ_DEFINE(tx_q, sizeof(struct can_frame), 32, 4);

static const struct device *const can_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_canbus));
static const struct device *const hw_wdt = DEVICE_DT_GET_OR_NULL(DT_ALIAS(watchdog0));

static atomic_t dropped_evts;
static atomic_t dropped_tx;

/* ---- producers: timer and CAN receive (interrupt context, must not block) ---- */

static void post(const struct bms_evt *e)
{
	if (k_msgq_put(&evt_q, e, K_NO_WAIT) != 0) {
		atomic_inc(&dropped_evts);
	}
}

static void tick_fn(struct k_timer *t)
{
	ARG_UNUSED(t);
	post(&(struct bms_evt){.type = EVT_TICK});
}

K_TIMER_DEFINE(tick_timer, tick_fn, NULL);

static void can_rx_cb(const struct device *dev, struct can_frame *f, void *user)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(user);
	if (f->id == ID_COMMAND && f->dlc >= 1) {
		post(&(struct bms_evt){.type = EVT_CMD, .cmd = f->data[0],
				       .token = f->dlc >= 2 ? f->data[1] : 0});
	} else if (f->id == ID_EDGE_HB) {
		post(&(struct bms_evt){.type = EVT_EDGE});
	}
}

static void can_state_cb(const struct device *dev, enum can_state state,
			 struct can_bus_err_cnt err, void *user)
{
	ARG_UNUSED(dev);
	ARG_UNUSED(user);
	LOG_WRN("CAN controller state %d (tx_err %u, rx_err %u)", state, err.tx_err_cnt,
		err.rx_err_cnt);
}

/* ---- frame building ---- */

static void queue_frame(uint32_t id, const uint8_t *data, uint8_t len)
{
	struct can_frame f = {.id = id, .dlc = len};

	memcpy(f.data, data, len);
	if (k_msgq_put(&tx_q, &f, K_NO_WAIT) != 0) {
		atomic_inc(&dropped_tx);
	}
}

static void send_status(const struct pack *p, bool edge_alive, uint8_t counter)
{
	uint8_t d[8];
	int16_t current_da = (int16_t)(p->current_a * 10.0f);
	int soc_half = (int)(p->soc * 200.0f + 0.5f);

	sys_put_le16((uint16_t)(pack_voltage_mv(p) / 10), &d[0]);
	sys_put_le16((uint16_t)current_da, &d[2]);
	d[4] = (uint8_t)CLAMP(soc_half, 0, 200);
	d[5] = (p->state & 0x0F) | (p->contactor << 4) | (p->balancing << 5) |
	       ((p->faults != 0) << 6) | (edge_alive << 7);
	d[6] = p->faults;
	d[7] = counter;
	queue_frame(ID_STATUS, d, 8);
}

static void send_cell_summary(const struct pack *p)
{
	uint8_t d[7];
	int lo, hi;

	pack_cell_min_max(p, &lo, &hi);
	sys_put_le16(p->cell_mv[lo], &d[0]);
	sys_put_le16(p->cell_mv[hi], &d[2]);
	d[4] = lo + 1;
	d[5] = hi + 1;
	d[6] = p->balancing_mask;
	queue_frame(ID_CELL_SUMMARY, d, 7);
}

static uint8_t temp_byte(float c)
{
	return (uint8_t)CLAMP((int)(c + 40.5f), 0, 255); /* offset -40 degC */
}

static void send_temperatures(const struct pack *p)
{
	uint8_t d[6];
	int t = pack_temp_max(p);

	for (int i = 0; i < PACK_TEMPS; i++) {
		d[i] = temp_byte(p->temp_c[i]);
	}
	d[4] = temp_byte(p->temp_c[t]);
	d[5] = t + 1;
	queue_frame(ID_TEMPERATURES, d, 6);
}

static void send_cell_voltages(const struct pack *p)
{
	/* Multiplexed: byte 0 is the group, then up to three cells. */
	for (int g = 0; g * 3 < PACK_CELLS; g++) {
		uint8_t d[8] = {g};
		int n = MIN(3, PACK_CELLS - g * 3);

		for (int k = 0; k < n; k++) {
			sys_put_le16(p->cell_mv[g * 3 + k], &d[1 + 2 * k]);
		}
		queue_frame(ID_CELL_VOLTS, d, 1 + 2 * n);
	}
}

static void send_heartbeat(uint32_t seq)
{
	uint8_t d[8];

	sys_put_le32(seq, &d[0]);
	sys_put_le32((uint32_t)(k_uptime_get() / 1000), &d[4]);
	queue_frame(ID_HEARTBEAT, d, 8);
}

static void send_fault(const struct pack *p, uint8_t trigger, uint8_t seq)
{
	uint8_t d[6] = {p->faults, trigger, p->fault_index + 1};

	sys_put_le16((uint16_t)p->fault_value, &d[3]);
	d[5] = seq;
	queue_frame(ID_FAULT, d, 6);
}

static void send_ack(uint8_t cmd, uint8_t token, uint8_t result, uint8_t state)
{
	uint8_t d[4] = {cmd, token, result, state};

	queue_frame(ID_COMMAND_ACK, d, 4);
}

/* ---- the watchdog ---- */

static void wdt_expired(int channel_id, void *user)
{
	/* Called from the task watchdog's timer (interrupt context). Switch
	 * logging to synchronous so this line gets out before the reset. */
	LOG_PANIC();
	LOG_ERR("task watchdog: thread '%s' (channel %d) stopped feeding, rebooting",
		k_thread_name_get((k_tid_t)user), channel_id);
	sys_reboot(SYS_REBOOT_COLD);
}

/* ---- threads ---- */

static void bms_thread(void *a, void *b, void *c)
{
	struct pack pack;
	uint32_t tick = 0, hb_seq = 0;
	uint8_t fault_seq = 0;
	int64_t edge_seen_ms = -EDGE_TIMEOUT_MS;
	enum bms_state last_state;
	int wdt = task_wdt_add(BMS_WDT_MS, wdt_expired, k_current_get());

	pack_init(&pack);
	last_state = pack.state;
	LOG_INF("pack: %d cells, SOC %d%%, state %s", PACK_CELLS, (int)(pack.soc * 100),
		pack_state_name(pack.state));

	for (;;) {
		struct bms_evt e;

		k_msgq_get(&evt_q, &e, K_FOREVER);
		task_wdt_feed(wdt);

		if (e.type == EVT_EDGE) {
			if (k_uptime_get() - edge_seen_ms > EDGE_TIMEOUT_MS) {
				LOG_INF("edge controller heartbeat seen (0x%03x)", ID_EDGE_HB);
			}
			edge_seen_ms = k_uptime_get();
			continue;
		}

		if (e.type == EVT_CMD) {
			if (e.cmd == CMD_SIM_HANG) {
				LOG_WRN("cmd 0x%02x SimHang: BMS thread stops for 5 s", e.cmd);
				send_ack(e.cmd, e.token, RESULT_OK, pack.state);
				k_sleep(K_SECONDS(5)); /* the task watchdog fires after 1 s */
				continue;
			}
			enum cmd_result r = pack_command(&pack, e.cmd);

			LOG_INF("cmd 0x%02x token %u -> %s, state %s", e.cmd, e.token,
				r == RESULT_OK ? "ok" : r == RESULT_REJECTED ? "rejected" : "unknown",
				pack_state_name(pack.state));
			send_ack(e.cmd, e.token, r, pack.state);
		} else { /* EVT_TICK */
			tick++;
			pack_step(&pack, TICK_MS / 1000.0f);

			if (pack.new_fault) {
				LOG_ERR("FAULT 0x%02x raised: index %u value %d, contactor opened",
					pack.new_fault, pack.fault_index + 1, pack.fault_value);
				send_fault(&pack, pack.new_fault, fault_seq++);
			} else if (pack.faults && tick % 10 == 0) {
				send_fault(&pack, 0, fault_seq++);
			}

			bool edge_alive = k_uptime_get() - edge_seen_ms < EDGE_TIMEOUT_MS;

			send_status(&pack, edge_alive, (uint8_t)tick);
			if (tick % 5 == 0) {
				send_cell_summary(&pack);
				send_temperatures(&pack);
			}
			if (tick % 10 == 0) {
				send_cell_voltages(&pack);
				send_heartbeat(hb_seq++);
			}
			if (tick % 100 == 0) {
				int lo, hi;

				pack_cell_min_max(&pack, &lo, &hi);
				LOG_INF("%s V=%u mV I=%d dA SOC=%d%% cells %u..%u mV Tmax=%d C "
					"edge=%d dropped evt/tx %ld/%ld",
					pack_state_name(pack.state), pack_voltage_mv(&pack),
					(int)(pack.current_a * 10), (int)(pack.soc * 100),
					pack.cell_mv[lo], pack.cell_mv[hi],
					(int)pack.temp_c[pack_temp_max(&pack)], edge_alive,
					atomic_get(&dropped_evts), atomic_get(&dropped_tx));
			}
		}

		if (pack.state != last_state) {
			LOG_INF("state %s -> %s", pack_state_name(last_state),
				pack_state_name(pack.state));
			last_state = pack.state;
		}
	}
}

static void can_tx_thread(void *a, void *b, void *c)
{
	int wdt = task_wdt_add(TX_WDT_MS, wdt_expired, k_current_get());
	uint32_t errors = 0;

	for (;;) {
		struct can_frame f;

		k_msgq_get(&tx_q, &f, K_FOREVER);
		int err = can_send(can_dev, &f, K_MSEC(100), NULL, NULL);

		if (err != 0 && (errors++ % 50) == 0) {
			LOG_WRN("can_send 0x%03x failed: %d (%u errors so far)", f.id, err, errors);
		}
		task_wdt_feed(wdt);
	}
}

K_THREAD_DEFINE(bms_tid, 2048, bms_thread, NULL, NULL, NULL, 5, 0, SYS_FOREVER_MS);
K_THREAD_DEFINE(can_tx_tid, 1024, can_tx_thread, NULL, NULL, NULL, 4, 0, SYS_FOREVER_MS);

/* ---- start-up ---- */

int main(void)
{
	int err;

	LOG_INF("zephyr-bms on %s, CAN ids 0x%03x-0x%03x", CONFIG_BOARD, ID_STATUS, ID_FAULT);

	if (!device_is_ready(can_dev)) {
		LOG_ERR("CAN device %s not ready", can_dev->name);
		return 0;
	}
	/* The edge controller's command, and its heartbeat (only to report
	 * EdgeAlive). Everything else on the bus is filtered out in hardware. */
	struct can_filter cmd_filter = {.id = ID_COMMAND, .mask = CAN_STD_ID_MASK};
	struct can_filter edge_filter = {.id = ID_EDGE_HB, .mask = CAN_STD_ID_MASK};

	if (can_add_rx_filter(can_dev, can_rx_cb, NULL, &cmd_filter) < 0 ||
	    can_add_rx_filter(can_dev, can_rx_cb, NULL, &edge_filter) < 0) {
		LOG_ERR("can_add_rx_filter failed");
		return 0;
	}
	can_set_state_change_callback(can_dev, can_state_cb, NULL);

	err = can_start(can_dev);
	if (err) {
		LOG_ERR("can_start: %d", err);
		return 0;
	}
	LOG_INF("CAN %s started", can_dev->name);

	/* The task watchdog multiplexes our two threads onto the one hardware
	 * watchdog: if any channel is not fed in time, its callback runs; if the
	 * task watchdog itself stops, the IWDG resets the chip. */
	err = task_wdt_init(hw_wdt);
	if (err) {
		LOG_ERR("task_wdt_init: %d", err);
		return 0;
	}
	LOG_INF("task watchdog on %s", hw_wdt ? hw_wdt->name : "(no hardware fallback)");

	k_thread_name_set(bms_tid, "bms");
	k_thread_name_set(can_tx_tid, "can_tx");
	k_thread_start(can_tx_tid);
	k_thread_start(bms_tid);
	k_timer_start(&tick_timer, K_MSEC(TICK_MS), K_MSEC(TICK_MS));
	return 0;
}
