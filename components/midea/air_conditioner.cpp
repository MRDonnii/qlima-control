#if (defined(USE_ARDUINO) && !defined(USE_RP2) && !defined(USE_LIBRETINY)) || defined(USE_ESP_IDF)

#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include "air_conditioner.h"
#include "ac_adapter.h"
#include <cmath>
#include <cstdint>

namespace esphome::midea::ac {

static void set_sensor(Sensor *sensor, float value) {
  if (sensor != nullptr && (!sensor->has_state() || sensor->get_raw_state() != value))
    sensor->publish_state(value);
}

// Like set_sensor, but NAN ("not reported") is only published once.
static void set_value(Sensor *sensor, float value) {
  if (sensor == nullptr)
    return;
  if (sensor->has_state() && std::isnan(value) && std::isnan(sensor->get_raw_state()))
    return;
  set_sensor(sensor, value);
}

static void set_binary(binary_sensor::BinarySensor *sensor, bool value) {
  if (sensor != nullptr && (!sensor->has_state() || sensor->state != value))
    sensor->publish_state(value);
}

// Qlima Control: engineering group data, asked for in turn.
// 1 compressor and refrigerant circuit, 5 defrost and outdoor fan, 2 indoor fan, 7 outdoor unit power.
static const uint8_t TELEMETRY_GROUPS[] = {1, 5, 2, 7};
static const uint8_t NUM_TELEMETRY_GROUPS = sizeof(TELEMETRY_GROUPS);

template<typename T> void update_property(T &property, const T &value, bool &flag) {
  if (property != value) {
    property = value;
    flag = true;
  }
}

void AirConditioner::on_status_change() {
  // Add frost protection custom preset once when autoconf completes
  if (this->base_.getAutoconfStatus() == dudanov::midea::AUTOCONF_OK &&
      this->base_.getCapabilities().supportFrostProtectionPreset() && !this->frost_protection_set_) {
    // Read existing presets (set by codegen), append frost protection, write back
    auto traits = this->get_traits();
    const auto &existing = traits.get_supported_custom_presets();
    bool found = false;
    for (const char *p : existing) {
      if (strcmp(p, Constants::FREEZE_PROTECTION) == 0) {
        found = true;
        break;
      }
    }
    if (!found) {
      std::vector<const char *> merged(existing.begin(), existing.end());
      merged.push_back(Constants::FREEZE_PROTECTION);
      this->set_supported_custom_presets(merged);
    }
    this->frost_protection_set_ = true;
  }
  bool need_publish = false;
  update_property(this->target_temperature, this->base_.getTargetTemp(), need_publish);
  update_property(this->current_temperature, this->base_.getIndoorTemp(), need_publish);
  auto mode = Converters::to_climate_mode(this->base_.getMode());
  update_property(this->mode, mode, need_publish);
  auto swing_mode = Converters::to_climate_swing_mode(this->base_.getSwingMode());
  update_property(this->swing_mode, swing_mode, need_publish);
  // Preset
  auto preset = this->base_.getPreset();
  if (Converters::is_custom_midea_preset(preset)) {
    if (this->set_custom_preset_(Converters::to_custom_climate_preset(preset)))
      need_publish = true;
  } else if (this->set_preset_(Converters::to_climate_preset(preset))) {
    need_publish = true;
  }
  // Fan mode
  auto fan_mode = this->base_.getFanMode();
  if (Converters::is_custom_midea_fan_mode(fan_mode)) {
    if (this->set_custom_fan_mode_(Converters::to_custom_climate_fan_mode(fan_mode)))
      need_publish = true;
  } else if (this->set_fan_mode_(Converters::to_climate_fan_mode(fan_mode))) {
    need_publish = true;
  }
  if (need_publish)
    this->publish_state();
  set_sensor(this->outdoor_sensor_, this->base_.getOutdoorTemp());
  set_sensor(this->power_sensor_, this->base_.getPowerUsage());
  set_sensor(this->humidity_sensor_, this->base_.getIndoorHum());
  set_sensor(this->error_code_sensor_, this->base_.getErrorCode());
  set_binary(this->filter_alert_sensor_, this->base_.getFilterAlert());
}

/* TELEMETRY (Qlima Control) */

bool AirConditioner::wants_group_(uint8_t group) const {
  switch (group) {
    case 1:
      return this->compressor_frequency_sensor_ != nullptr || this->compressor_target_frequency_sensor_ != nullptr ||
             this->indoor_coil_sensor_ != nullptr || this->outdoor_coil_sensor_ != nullptr ||
             this->discharge_sensor_ != nullptr;
    case 2:
      return this->indoor_fan_sensor_ != nullptr;
    case 5:
      return this->defrost_sensor_ != nullptr || this->outdoor_fan_sensor_ != nullptr;
    case 7:
      return this->outdoor_power_sensor_ != nullptr;
    default:
      return false;
  }
}

void AirConditioner::setup() {
  ApplianceBase<dudanov::midea::ac::AirConditioner>::setup();
  bool any = false;
  for (uint8_t g : TELEMETRY_GROUPS)
    any |= this->wants_group_(g);
  if (!any)
    return;
  this->publish_groups_();
  // Let autoconf and the first status polls settle before asking for group data.
  this->set_timeout("telemetry_start", 30000, [this]() {
    this->set_interval("telemetry", this->telemetry_interval_, [this]() { this->query_next_group_(); });
  });
}

void AirConditioner::query_next_group_() {
  if (this->group_pending_) {
    if (millis() - this->group_pending_since_ < 30000)
      return;
    this->group_pending_ = false;  // lost request: move on
  }
  for (uint8_t k = 0; k < NUM_TELEMETRY_GROUPS; k++) {
    const uint8_t g = TELEMETRY_GROUPS[(this->next_group_ + k) % NUM_TELEMETRY_GROUPS];
    if (!this->wants_group_(g) || this->group_fails_[g] == GROUP_DEAD)
      continue;
    this->next_group_ = (this->next_group_ + k + 1) % NUM_TELEMETRY_GROUPS;
    this->group_pending_ = true;
    this->group_pending_since_ = millis();
    this->base_.queryGroup(
        g,
        [this, g](const dudanov::midea::FrameData &data) {
          this->group_pending_ = false;
          this->on_group_data_(g, data.data(), data.size());
        },
        [this, g]() {
          this->group_pending_ = false;
          this->on_group_error_(g);
        });
    return;
  }
}

void AirConditioner::on_group_error_(uint8_t group) {
  if (this->group_fails_[group] == GROUP_DEAD)
    return;
  this->group_fails_[group]++;
  if (!this->group_ok_[group] && this->group_fails_[group] >= 3) {
    // Support is model-dependent and not in the capabilities report: three strikes and it is skipped.
    this->group_fails_[group] = GROUP_DEAD;
    ESP_LOGI(Constants::TAG, "Group data %u: no answer from this unit, not asking again", group);
    this->publish_groups_();
  } else if (this->group_ok_[group] && this->group_fails_[group] == 3) {
    this->clear_group_(group);
  }
}

void AirConditioner::clear_group_(uint8_t group) {
  switch (group) {
    case 1:
      set_value(this->compressor_frequency_sensor_, NAN);
      set_value(this->compressor_target_frequency_sensor_, NAN);
      set_value(this->indoor_coil_sensor_, NAN);
      set_value(this->outdoor_coil_sensor_, NAN);
      set_value(this->discharge_sensor_, NAN);
      break;
    case 2:
      set_value(this->indoor_fan_sensor_, NAN);
      break;
    case 5:
      set_value(this->outdoor_fan_sensor_, NAN);
      break;
    case 7:
      set_value(this->outdoor_power_sensor_, NAN);
      break;
  }
}

void AirConditioner::on_group_data_(uint8_t group, const uint8_t *d, uint8_t n) {
  const bool was_ok = this->group_ok_[group];
  const bool was_real = this->group_real_[group];
  this->group_ok_[group] = true;
  this->group_fails_[group] = 0;
  // Some models answer every group but zero-fill the ones they do not support.
  // A group only counts once it has carried real data.
  switch (group) {
    case 1: {  // compressor and refrigerant circuit
      if (n < 15)
        return;
      if (d[11] != 0 || d[12] != 0)
        this->group_real_[1] = true;
      if (!this->group_real_[1])
        break;
      const uint8_t hz = d[4];
      this->last_hz_ = hz;
      set_value(this->compressor_frequency_sensor_, hz);
      // A zero target while the compressor runs means "not reported".
      set_value(this->compressor_target_frequency_sensor_, (d[5] != 0 || hz == 0) ? (float) d[5] : NAN);
      set_value(this->indoor_coil_sensor_, d[11] ? (d[11] - 30) / 2.0f : NAN);   // T2
      set_value(this->outdoor_coil_sensor_, d[12] ? (d[12] - 50) / 2.0f : NAN);  // T3
      set_value(this->discharge_sensor_, d[14] ? (float) d[14] : NAN);           // TP
      break;
    }
    case 2:  // indoor fan
      if (n < 6)
        return;
      if (d[4] != 0 || d[5] != 0)
        this->group_real_[2] = true;
      if (this->group_real_[2])
        set_value(this->indoor_fan_sensor_, d[5] * 8.0f);
      break;
    case 5:  // humidity, outdoor fan, defrost
      if (n < 11)
        return;
      if (d[8] != 0 || d[10] != 0)
        this->group_real_[5] = true;
      if (this->group_real_[5]) {
        set_value(this->outdoor_fan_sensor_, d[8] * 8.0f);
        set_binary(this->defrost_sensor_, d[10] != 0);
      }
      break;
    case 7: {  // outdoor unit input power
      if (n < 12)
        return;
      const uint16_t watts = d[10] | (d[11] << 8);
      if (watts != 0)
        this->group_real_[7] = true;
      if (this->group_real_[7])
        set_value(this->outdoor_power_sensor_, watts);
      break;
    }
  }
  if (this->group_real_[group] != was_real)
    ESP_LOGI(Constants::TAG, "Group data %u: the unit reports real values", group);
  if (this->group_real_[group] != was_real || !was_ok)
    this->publish_groups_();
}

void AirConditioner::publish_groups_() {
  if (this->telemetry_groups_sensor_ == nullptr)
    return;
  // e.g. "1, 5 · kun nuller indtil videre: 7 · intet svar: 2"
  std::string real, zero, dead;
  bool probing = false;
  for (uint8_t g : TELEMETRY_GROUPS) {
    if (!this->wants_group_(g))
      continue;
    const std::string name = std::to_string(g);
    if (this->group_real_[g])
      real += (real.empty() ? "" : ", ") + name;
    else if (this->group_fails_[g] == GROUP_DEAD)
      dead += (dead.empty() ? "" : ", ") + name;
    else if (this->group_ok_[g])
      zero += (zero.empty() ? "" : ", ") + name;  // answers, but only zeros so far (e.g. compressor not run yet)
    else
      probing = true;
  }
  std::string text = real;
  if (!zero.empty())
    text += (text.empty() ? "" : " · ") + std::string("kun nuller indtil videre: ") + zero;
  if (!dead.empty())
    text += (text.empty() ? "" : " · ") + std::string("intet svar: ") + dead;
  if (text.empty())
    text = probing ? "spørger anlægget" : "ingen";
  this->telemetry_groups_sensor_->publish_state(text);
}

void AirConditioner::control(const ClimateCall &call) {
  dudanov::midea::ac::Control ctrl{};
  auto target_temp_val = call.get_target_temperature();
  if (target_temp_val.has_value())
    ctrl.targetTemp = *target_temp_val;
  auto swing_mode_val = call.get_swing_mode();
  if (swing_mode_val.has_value())
    ctrl.swingMode = Converters::to_midea_swing_mode(*swing_mode_val);
  auto mode_val = call.get_mode();
  if (mode_val.has_value())
    ctrl.mode = Converters::to_midea_mode(*mode_val);
  auto preset_val = call.get_preset();
  if (preset_val.has_value()) {
    ctrl.preset = Converters::to_midea_preset(*preset_val);
  } else if (call.has_custom_preset()) {
    // get_custom_preset() returns StringRef pointing to null-terminated string literals from codegen
    ctrl.preset = Converters::to_midea_preset(call.get_custom_preset().c_str());
  }
  auto fan_mode_val = call.get_fan_mode();
  if (fan_mode_val.has_value()) {
    ctrl.fanMode = Converters::to_midea_fan_mode(*fan_mode_val);
  } else if (call.has_custom_fan_mode()) {
    // get_custom_fan_mode() returns StringRef pointing to null-terminated string literals from codegen
    ctrl.fanMode = Converters::to_midea_fan_mode(call.get_custom_fan_mode().c_str());
  }
  this->base_.control(ctrl);
}

ClimateTraits AirConditioner::traits() {
  auto traits = ClimateTraits();
  traits.add_feature_flags(climate::CLIMATE_SUPPORTS_CURRENT_TEMPERATURE);
  traits.set_visual_min_temperature(17);
  traits.set_visual_max_temperature(30);
  traits.set_visual_temperature_step(0.5);
  traits.set_supported_modes(this->supported_modes_);
  traits.set_supported_swing_modes(this->supported_swing_modes_);
  traits.set_supported_presets(this->supported_presets_);
  // Custom fan modes and presets are stored on Climate base class and wired via get_traits()
  /* + MINIMAL SET OF CAPABILITIES */
  traits.add_supported_fan_mode(ClimateFanMode::CLIMATE_FAN_AUTO);
  traits.add_supported_fan_mode(ClimateFanMode::CLIMATE_FAN_LOW);
  traits.add_supported_fan_mode(ClimateFanMode::CLIMATE_FAN_MEDIUM);
  traits.add_supported_fan_mode(ClimateFanMode::CLIMATE_FAN_HIGH);
  if (this->base_.getAutoconfStatus() == dudanov::midea::AUTOCONF_OK) {
    Converters::to_climate_traits(traits, this->base_.getCapabilities());
  }
  if (!traits.get_supported_modes().empty())
    traits.add_supported_mode(ClimateMode::CLIMATE_MODE_OFF);
  if (!traits.get_supported_swing_modes().empty())
    traits.add_supported_swing_mode(ClimateSwingMode::CLIMATE_SWING_OFF);
  if (!traits.get_supported_presets().empty())
    traits.add_supported_preset(ClimatePreset::CLIMATE_PRESET_NONE);
  return traits;
}

void AirConditioner::dump_config() {
  ESP_LOGCONFIG(Constants::TAG,
                "MideaDongle:\n"
                "  [x] Period: %" PRIu32 "ms\n"
                "  [x] Response timeout: %" PRIu32 "ms\n"
                "  [x] Request attempts: %d",
                this->base_.getPeriod(), this->base_.getTimeout(), this->base_.getNumAttempts());
#ifdef USE_REMOTE_TRANSMITTER
  ESP_LOGCONFIG(Constants::TAG, "  [x] Using RemoteTransmitter");
#endif
  if (this->base_.getAutoconfStatus() == dudanov::midea::AUTOCONF_OK) {
    this->base_.getCapabilities().dump();
  } else if (this->base_.getAutoconfStatus() == dudanov::midea::AUTOCONF_ERROR) {
    ESP_LOGW(Constants::TAG,
             "Failed to get 0xB5 capabilities report. Suggest to disable it in config and manually set your "
             "appliance options.");
  }
  this->dump_traits_(Constants::TAG);
}

/* ACTIONS */

void AirConditioner::do_follow_me(float temperature, bool use_fahrenheit, bool beeper) {
#ifdef USE_REMOTE_TRANSMITTER
  // Check if temperature is finite (not NaN or infinite)
  if (!std::isfinite(temperature)) {
    ESP_LOGW(Constants::TAG, "Follow me action requires a finite temperature, got: %f", temperature);
    return;
  }

  // Round and convert temperature to long, then clamp and convert it to uint8_t
  uint8_t temp_uint8 =
      static_cast<uint8_t>(esphome::clamp<long>(std::lroundf(temperature), 0L, static_cast<long>(UINT8_MAX)));

  char temp_symbol = use_fahrenheit ? 'F' : 'C';
  ESP_LOGD(Constants::TAG, "Follow me action called with temperature: %.5f °%c, rounded to: %u °%c", temperature,
           temp_symbol, temp_uint8, temp_symbol);

  // Create and transmit the data
  IrFollowMeData data(temp_uint8, use_fahrenheit, beeper);
  this->transmitter_.transmit(data);
#else
  ESP_LOGW(Constants::TAG, "Action needs remote_transmitter component");
#endif
}

void AirConditioner::do_swing_step() {
#ifdef USE_REMOTE_TRANSMITTER
  IrSpecialData data(0x01);
  this->transmitter_.transmit(data);
#else
  ESP_LOGW(Constants::TAG, "Action needs remote_transmitter component");
#endif
}

void AirConditioner::do_display_toggle() {
  if (this->base_.getCapabilities().supportLightControl()) {
    this->base_.displayToggle();
  } else {
#ifdef USE_REMOTE_TRANSMITTER
    IrSpecialData data(0x08);
    this->transmitter_.transmit(data);
#else
    ESP_LOGW(Constants::TAG, "Action needs remote_transmitter component");
#endif
  }
}

}  // namespace esphome::midea::ac

#endif  // USE_ARDUINO || USE_ESP_IDF
