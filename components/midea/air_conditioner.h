#pragma once

#if (defined(USE_ARDUINO) && !defined(USE_RP2) && !defined(USE_LIBRETINY)) || defined(USE_ESP_IDF)

// MideaUART
#include <Appliance/AirConditioner/AirConditioner.h>

#include "appliance_base.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/text_sensor/text_sensor.h"

namespace esphome::midea::ac {

using sensor::Sensor;
using climate::ClimateCall;
using climate::ClimatePreset;
using climate::ClimateTraits;
using climate::ClimateMode;
using climate::ClimateSwingMode;
using climate::ClimateFanMode;
using climate::ClimateModeMask;
using climate::ClimateSwingModeMask;
using climate::ClimatePresetMask;

class AirConditioner final : public ApplianceBase<dudanov::midea::ac::AirConditioner>, public climate::Climate {
 public:
  void dump_config() override;
  void set_outdoor_temperature_sensor(Sensor *sensor) { this->outdoor_sensor_ = sensor; }
  void set_humidity_setpoint_sensor(Sensor *sensor) { this->humidity_sensor_ = sensor; }
  void set_power_sensor(Sensor *sensor) { this->power_sensor_ = sensor; }
  void on_status_change() override;
  void setup() override;

  /* Qlima Control: the unit's engineering telemetry ("group data") and status extras */
  void set_power_query(bool state) { this->base_.setPowerQuery(state); }
  void set_telemetry_interval(uint32_t ms) { this->telemetry_interval_ = ms; }
  void set_compressor_frequency_sensor(Sensor *s) { this->compressor_frequency_sensor_ = s; }
  void set_compressor_target_frequency_sensor(Sensor *s) { this->compressor_target_frequency_sensor_ = s; }
  void set_compressor_current_sensor(Sensor *s) { this->compressor_current_sensor_ = s; }
  void set_indoor_coil_temperature_sensor(Sensor *s) { this->indoor_coil_sensor_ = s; }
  void set_outdoor_coil_temperature_sensor(Sensor *s) { this->outdoor_coil_sensor_ = s; }
  void set_discharge_temperature_sensor(Sensor *s) { this->discharge_sensor_ = s; }
  void set_indoor_fan_speed_sensor(Sensor *s) { this->indoor_fan_sensor_ = s; }
  void set_outdoor_fan_speed_sensor(Sensor *s) { this->outdoor_fan_sensor_ = s; }
  void set_outdoor_unit_power_sensor(Sensor *s) { this->outdoor_power_sensor_ = s; }
  void set_defrost_sensor(binary_sensor::BinarySensor *s) { this->defrost_sensor_ = s; }
  void set_error_code_sensor(Sensor *s) { this->error_code_sensor_ = s; }
  void set_filter_alert_sensor(binary_sensor::BinarySensor *s) { this->filter_alert_sensor_ = s; }
  void set_telemetry_groups_sensor(text_sensor::TextSensor *s) { this->telemetry_groups_sensor_ = s; }

  /* ############### */
  /* ### ACTIONS ### */
  /* ############### */

  void do_follow_me(float temperature, bool use_fahrenheit, bool beeper = false);
  void do_display_toggle();
  void do_swing_step();
  void do_beeper_on() { this->set_beeper_feedback(true); }
  void do_beeper_off() { this->set_beeper_feedback(false); }
  void do_power_on() { this->base_.setPowerState(true); }
  void do_power_off() { this->base_.setPowerState(false); }
  void do_power_toggle() { this->base_.setPowerState(this->mode == ClimateMode::CLIMATE_MODE_OFF); }
  void set_supported_modes(ClimateModeMask modes) { this->supported_modes_ = modes; }
  void set_supported_swing_modes(ClimateSwingModeMask modes) { this->supported_swing_modes_ = modes; }
  void set_supported_presets(ClimatePresetMask presets) { this->supported_presets_ = presets; }
  void set_custom_presets(std::initializer_list<const char *> presets) { this->set_supported_custom_presets(presets); }
  void set_custom_fan_modes(std::initializer_list<const char *> modes) { this->set_supported_custom_fan_modes(modes); }

 protected:
  void control(const ClimateCall &call) override;
  ClimateTraits traits() override;
  ClimateModeMask supported_modes_{};
  ClimateSwingModeMask supported_swing_modes_{};
  ClimatePresetMask supported_presets_{};
  bool frost_protection_set_{false};
  Sensor *outdoor_sensor_{nullptr};
  Sensor *humidity_sensor_{nullptr};
  Sensor *power_sensor_{nullptr};

  // Telemetry
  static constexpr uint8_t NUM_GROUPS = 8;
  static constexpr int8_t GROUP_DEAD = 127;
  void query_next_group_();
  void on_group_data_(uint8_t group, const uint8_t *d, uint8_t n);
  void on_group_error_(uint8_t group);
  void clear_group_(uint8_t group);
  bool wants_group_(uint8_t group) const;
  void publish_groups_();
  uint32_t telemetry_interval_{10000};
  uint8_t next_group_{0};
  bool group_pending_{false};
  uint32_t group_pending_since_{0};
  bool group_ok_[NUM_GROUPS]{};     // answered at least once
  bool group_real_[NUM_GROUPS]{};   // answered with non-zero data (some models zero-fill groups)
  int8_t group_fails_[NUM_GROUPS]{};
  uint8_t last_hz_{0};
  Sensor *compressor_frequency_sensor_{nullptr};
  Sensor *compressor_target_frequency_sensor_{nullptr};
  Sensor *compressor_current_sensor_{nullptr};
  Sensor *indoor_coil_sensor_{nullptr};
  Sensor *outdoor_coil_sensor_{nullptr};
  Sensor *discharge_sensor_{nullptr};
  Sensor *indoor_fan_sensor_{nullptr};
  Sensor *outdoor_fan_sensor_{nullptr};
  Sensor *outdoor_power_sensor_{nullptr};
  binary_sensor::BinarySensor *defrost_sensor_{nullptr};
  Sensor *error_code_sensor_{nullptr};
  binary_sensor::BinarySensor *filter_alert_sensor_{nullptr};
  text_sensor::TextSensor *telemetry_groups_sensor_{nullptr};
};

}  // namespace esphome::midea::ac

#endif  // USE_ARDUINO || USE_ESP_IDF
