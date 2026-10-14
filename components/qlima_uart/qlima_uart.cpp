#include "qlima_uart.h"

#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#ifdef USE_ESP32
#include <driver/gpio.h>
#include <driver/uart.h>
#include <hal/gpio_ll.h>
#include <soc/gpio_struct.h>
#include "esphome/components/uart/uart_component_esp_idf.h"
#endif

namespace esphome {
namespace qlima_uart {

static const char *const TAG = "qlima_uart";

void UartAutoSwap::setup() {
  // Stores the TX pin that worked last time, so a reflash with other pins starts by the book.
  this->pref_ = global_preferences->make_preference<uint8_t>(fnv1_hash("qlima_uart_tx"));
  uint8_t tx = 0;
  if (this->pref_.load(&tx)) {
    this->saved_tx_ = tx;
    if (tx == this->rx_) {
      this->swapped_ = true;
      this->apply_();
    }
  }
  this->probe_at_ = millis();
}

void UartAutoSwap::loop() {
  if (this->found_ || this->gave_up_)
    return;
  if (this->answered_ && this->answered_()) {
    this->found_ = true;
    const uint8_t tx = this->swapped_ ? this->rx_ : this->tx_;
    if (tx != this->saved_tx_) {
      this->pref_.save(&tx);
      this->saved_tx_ = tx;
    }
    ESP_LOGI(TAG, "Unit answers with TX on GPIO%u, RX on GPIO%u%s", tx, this->swapped_ ? this->tx_ : this->rx_,
             this->swapped_ ? " (wires the other way round – swapped)" : "");
    return;
  }
  if (millis() - this->probe_at_ < this->probe_ms_)
    return;
  if (this->switches_ >= this->max_switches_) {
    // Neither way answers (unit without power, broken wire …): go back to the saved/by-the-book way.
    this->gave_up_ = true;
    const bool want = this->saved_tx_ == this->rx_;
    if (want != this->swapped_) {
      this->swapped_ = want;
      this->apply_();
    }
    ESP_LOGW(TAG, "The unit answers on neither TX/RX way – check the wiring. Keeping TX on GPIO%u",
             this->swapped_ ? this->rx_ : this->tx_);
    return;
  }
  this->switches_++;
  this->swapped_ = !this->swapped_;
  ESP_LOGW(TAG, "No answer from the unit – trying TX/RX the other way round (%u/%u)", this->switches_,
           this->max_switches_);
  this->apply_();
  this->probe_at_ = millis();
}

void UartAutoSwap::apply_() {
#ifdef USE_ESP32
  auto *idf = static_cast<uart::IDFUARTComponent *>(this->uart_);
  const auto port = static_cast<uart_port_t>(idf->get_hw_serial_number());
  const auto tx = static_cast<gpio_num_t>(this->swapped_ ? this->rx_ : this->tx_);
  const auto rx = static_cast<gpio_num_t>(this->swapped_ ? this->tx_ : this->rx_);
  // RX: input with pull-up. TX: open drain, so the unit's 5 V pull-up sets the high level.
  gpio_pullup_dis(tx);
  gpio_pullup_en(rx);
  gpio_ll_od_disable(&GPIO, rx);
  esp_err_t err = uart_set_pin(port, tx, rx, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
  gpio_ll_od_enable(&GPIO, tx);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "uart_set_pin failed: %s", esp_err_to_name(err));
    return;
  }
  // Drop whatever came in on the old pins.
  uart_flush_input(port);
  ESP_LOGD(TAG, "UART TX GPIO%d, RX GPIO%d", tx, rx);
#endif
}

void UartAutoSwap::dump_config() {
  ESP_LOGCONFIG(TAG, "UART auto TX/RX:");
  ESP_LOGCONFIG(TAG, "  By the book: TX GPIO%u, RX GPIO%u", this->tx_, this->rx_);
  ESP_LOGCONFIG(TAG, "  Now: %s", this->swapped_ ? "swapped" : "by the book");
  ESP_LOGCONFIG(TAG, "  Probe interval: %u ms, max switches: %u", this->probe_ms_, this->max_switches_);
}

}  // namespace qlima_uart
}  // namespace esphome
