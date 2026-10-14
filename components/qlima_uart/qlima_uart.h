#pragma once

#include <functional>

#include "esphome/core/component.h"
#include "esphome/core/preferences.h"
#include "esphome/components/uart/uart.h"

namespace esphome {
namespace qlima_uart {

/// Swaps TX and RX at runtime while the unit does not answer, and remembers the
/// way that works. Used when the white and green wires may be the other way round.
class UartAutoSwap : public Component {
 public:
  void set_uart(uart::UARTComponent *uart) { this->uart_ = uart; }
  void set_pins(uint8_t tx, uint8_t rx) {
    this->tx_ = tx;
    this->rx_ = rx;
  }
  void set_answered(std::function<bool()> &&answered) { this->answered_ = std::move(answered); }
  void set_probe_interval(uint32_t ms) { this->probe_ms_ = ms; }
  void set_max_switches(uint8_t n) { this->max_switches_ = n; }

  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  /// True when TX/RX run the other way round than wired by the book.
  bool is_swapped() const { return this->swapped_; }
  /// True once the unit answered with the current pins.
  bool is_found() const { return this->found_; }
  /// True when the unit answered on neither way and the search stopped.
  bool gave_up() const { return this->gave_up_; }

 protected:
  void apply_();

  uart::UARTComponent *uart_{nullptr};
  std::function<bool()> answered_;
  ESPPreferenceObject pref_;
  uint32_t probe_ms_{20000};
  uint32_t probe_at_{0};
  uint8_t tx_{0};
  uint8_t rx_{0};
  uint8_t saved_tx_{0};
  uint8_t switches_{0};
  uint8_t max_switches_{6};
  bool swapped_{false};
  bool found_{false};
  bool gave_up_{false};
};

}  // namespace qlima_uart
}  // namespace esphome
