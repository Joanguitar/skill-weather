# Copyright 2017, Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Mycroft skill for communicating weather information

This skill uses the Open Weather Map API (https://openweathermap.org) and
the PyOWM wrapper for it.  For more info, see:

General info on PyOWM:
    https://www.slideshare.net/csparpa/pyowm-my-first-open-source-project
OWM doc for APIs used:
    https://openweathermap.org/current - current
    https://openweathermap.org/forecast5 - three hour forecast
    https://openweathermap.org/forecast16 - daily forecasts
PyOWM docs:
    https://media.readthedocs.org/pdf/pyowm/latest/pyowm.pdf
"""

from collections import defaultdict
from datetime import datetime, timedelta
from multi_key_dict import multi_key_dict
from time import sleep
from typing import List, Optional

from adapt.intent import IntentBuilder
import pytz
from requests import HTTPError

import mycroft.audio
from mycroft import MycroftSkill, intent_handler
from mycroft.messagebus.message import Message
from mycroft.util.format import (nice_date, join_list)
from mycroft.util.parse import extract_datetime, extract_number
from mycroft.util.time import to_utc, to_local
from .source import (
    get_sequence_of_days,
    LocationNotFoundError,
    OpenWeatherMapApi,
    OWMApi,
    WeatherConfig,
    WeatherDialog,
    WeatherIntent,
    WeatherReport
)

# TODO: VK Failures
#   Locations: Washington, D.C.
#
# TODO: Intent failures
#   Later weather: only the word "later" in the vocab file works all others
#       invoke datetime skill

MARK_II = "mycroft_mark_2"
MINUTES = 60  # Minutes to seconds multiplier
CLEAR = 0
PARTLY_CLOUDY = 1
CLOUDY = 2
LIGHT_RAIN = 3
RAIN = 4
THUNDERSTORM = 5
SNOW = 6
WINDY = 7


class WeatherSkill(MycroftSkill):
    def __init__(self):
        super().__init__("WeatherSkill")
        self.weather_api = OWMApi()
        self.weather_api_new = OpenWeatherMapApi()
        self.weather_config = WeatherConfig(self.config_core, self.settings)
        self.platform = self.config_core['enclosure']['platform']
        # Build a dictionary to translate OWM weather-conditions
        # codes into the Mycroft weather icon codes
        # (see https://openweathermap.org/weather-conditions)
        self.image_codes = multi_key_dict()
        self.image_codes['01d', '01n'] = CLEAR
        self.image_codes['02d', '02n', '03d', '03n'] = PARTLY_CLOUDY
        self.image_codes['04d', '04n'] = CLOUDY
        self.image_codes['09d', '09n'] = LIGHT_RAIN
        self.image_codes['10d', '10n'] = RAIN
        self.image_codes['11d', '11n'] = THUNDERSTORM
        self.image_codes['13d', '13n'] = SNOW
        self.image_codes['50d', '50n'] = WINDY

        # Use Mycroft proxy if no private key provided
        self.settings["api_key"] = None
        self.settings["use_proxy"] = True

    def initialize(self):
        if self.weather_api:
            self.weather_api.set_OWM_language(lang=OWMApi.get_language(self.lang))
        self.weather_config.speed_unit = self.translate(
            self.weather_config.speed_unit
        )
        self.weather_config.temperature_unit = self.translate(
            self.weather_config.temperature_unit
        )

    @intent_handler(
        IntentBuilder("").one_of("Weather", "Forecast").optionally("Query")
        .optionally("Location").optionally("Today")
    )
    def handle_current_weather(self, message):
        # Handle: what is the weather like?
        self._report_current_weather(message)

    @intent_handler(
        IntentBuilder("").require("Query").require("Like").require("Outside")
        .optionally("Location").optionally("Today")
    )
    def handle_like_outside(self, message):
        self._report_current_weather(message)

    @intent_handler("what.is.multi.day.forecast.intent")
    def handle_multi_day_forecast(self, message):
        """ Handler for three day forecast without specified location

        Examples:   "What is the 3 day forecast?"
                    "What is the weather forecast?"
        """
        if self.voc_match(message.data['num'], 'Couple'):
            days = 2
        else:
            days = int(extract_number(message.data['num']))
            if days > 7:
                self.speak_dialog('seven.days.available')
                days = 7
        self._report_multi_day_forecast(message, days)

    @intent_handler(
        IntentBuilder("").one_of("Weather", "Forecast").optionally("Query")
        .require("RelativeDay").optionally("Location")
    )
    def handle_one_day_forecast(self, message):
        # Handle: What is the weather forecast tomorrow?
        self._report_one_day_forecast(message)

    @intent_handler(
        IntentBuilder("").require("Query").require("Weather")
        .require("Later").optionally("Location")
    )
    def handle_next_hour(self, message):
        # Handle: What's the weather later?
        self._report_one_hour_weather(message)

    @intent_handler(
        IntentBuilder("").require("RelativeTime").one_of("Weather", "Forecast")
        .optionally("Query").optionally("RelativeDay").optionally("Location")
    )
    def handle_weather_at_time(self, message):
        # Handle: What's the weather tonight / tomorrow morning?
        self._report_one_hour_weather(message)

    @intent_handler(
        IntentBuilder("").require("Query").one_of("Weather", "Forecast")
        .require("Weekend").optionally("Location")
    )
    def handle_weekend_forecast(self, message):
        """ Handle weather for weekend. """
        self._report_weekend_forecast(message)

    @intent_handler(
        IntentBuilder("").optionally("Query").one_of("Weather", "Forecast")
        .require("Week").optionally("Location")
    )
    def handle_week_weather(self, message):
        """ Handle weather for week.
            Speaks overview of week, not daily forecasts """
        self._report_multi_day_forecast(message, days=7)

    @intent_handler(
        IntentBuilder("").require("Temperature").optionally("Query")
        .optionally("Location").optionally("Unit").optionally("Today")
        .optionally("Now")
    )
    def handle_current_temperature(self, message):
        self._report_temperature(message, temperature_type="current")

    @intent_handler(
        IntentBuilder("").optionally("Query").require("Temperature")
        .optionally("Location").optionally("Unit").optionally("RelativeDay")
        .optionally("Now")
    )
    def handle_simple_temperature(self, message):
        self._report_temperature(message, temperature_type="current")

    @intent_handler(
        IntentBuilder("").require("RelativeTime").require("Temperature")
        .optionally("Query").optionally("RelativeDay").optionally("Location")
    )
    def handle_temperature_at_time(self, message):
        self._report_temperature(message)

    @intent_handler(
        IntentBuilder("").optionally("Query").require("High")
        .optionally("Temperature").optionally("Location").optionally("Unit")
        .optionally("RelativeDay")
    )
    def handle_high_temperature(self, message):
        self._report_temperature(message, temperature_type="high")

    @intent_handler(
        IntentBuilder("").optionally("Query").require("Low")
        .optionally("Temperature").optionally("Location").optionally("Unit")
        .optionally("RelativeDay")
    )
    def handle_low_temperature(self, message):
        self._report_temperature(message, temperature_type="low")

    @intent_handler(
        IntentBuilder("").require("ConfirmQueryCurrent").one_of("Hot", "Cold")
        .optionally("Location").optionally("Today")
    )
    def handle_is_it_hot(self, message):
        """ Handler for utterances similar to
        is it hot today?, is it cold? etc
        """
        self._report_temperature(message, "current")

    @intent_handler(
        IntentBuilder("").optionally("How").one_of("Hot", "Cold")
        .one_of("ConfirmQueryFuture", "ConfirmQueryCurrent")
        .optionally("Location").optionally("RelativeDay")
    )
    def handle_how_hot_or_cold(self, message):
        """ Handler for utterances similar to
        how hot will it be today?, how cold will it be? , etc
        """
        temperature_type = "high" if message.data.get('Hot') else "low"
        self._report_temperature(message, temperature_type)

    @intent_handler(
        IntentBuilder("").require("How").one_of("Hot", "Cold")
        .one_of("ConfirmQueryFuture", "ConfirmQueryCurrent")
        .optionally("Location").optionally("RelativeDay")
    )
    def handle_how_hot_or_cold_alt(self, message):
        temperature_type = "high" if message.data.get('Hot') else "low"
        self._report_temperature(message, temperature_type)

    @intent_handler(
        IntentBuilder("").require("ConfirmQuery").require("Windy")
        .optionally("Location").optionally("RelativeDay")
    )
    def handle_is_it_windy(self, message):
        """ Handler for utterances similar to "is it windy today?" """
        self._report_wind(message)

    @intent_handler(
        IntentBuilder("").require("How").require("Windy")
        .optionally("Location").optionally("ConfirmQuery")
        .optionally("RelativeDay")
    )
    def handle_windy(self, message):
        # Handle: How windy is it?
        self._report_wind(message)

    @intent_handler(
        IntentBuilder("").require("ConfirmQuery").require("Snowing")
        .optionally("Location")
    )
    def handle_is_it_snowing(self, message):
        """Handler for utterances similar to "is it snowing today?" """
        self._report_weather_condition(message, "Snow")

    @intent_handler(
        IntentBuilder("").require("ConfirmQuery").require("Clear")
        .optionally("Location")
    )
    def handle_is_it_clear(self, message):
        """Handler for utterances similar to "is it clear skies today?" """
        self._report_weather_condition(message, condition="Clear")

    @intent_handler(
        IntentBuilder("").require("ConfirmQuery").require("Cloudy")
        .optionally("Location").optionally("RelativeTime")
    )
    def handle_is_it_cloudy(self, message):
        """Handler for utterances similar to "is it cloudy skies today?" """
        self._report_weather_condition(message, "Clouds")

    @intent_handler(
        IntentBuilder("").require("ConfirmQuery").require("Foggy")
        .optionally("Location")
    )
    def handle_is_it_foggy(self, message):
        """Handler for utterances similar to "is it foggy today?" """
        self._report_weather_condition(message, "Fog")

    @intent_handler(
        IntentBuilder("").require("ConfirmQuery").require("Raining")
        .optionally("Location")
    )
    def handle_is_it_raining(self, message):
        """Handler for utterances similar to "is it raining today?" """
        self._report_weather_condition(message, "Rain")

    @intent_handler("do.i.need.an.umbrella.intent")
    def handle_need_umbrella(self, message):
        self._report_weather_condition(message, "Rain")

    @intent_handler(
        IntentBuilder("").require("ConfirmQuery").require("Storm")
        .optionally("Location")
    )
    def handle_is_it_storming(self, message):
        """Handler for utterances similar to "is it storming today?" """
        self._report_weather_condition(message, 'Thunderstorm')

    @intent_handler(
        IntentBuilder("").require("When").optionally("Next")
        .require("Precipitation").optionally("Location")
    )
    def handle_next_precipitation(self, message):
        # Handle: When will it rain again?
        intent_data = WeatherIntent(message, self.lang)
        weather = self._get_weather(intent_data)
        if weather is not None:
            forecast, timeframe = weather.get_next_precipitation(intent_data)
            intent_data.timeframe = timeframe
            dialog = WeatherDialog(forecast, self.weather_config, intent_data)
            dialog.build_next_precipitation_dialog()
            self._speak_weather(dialog)

    @intent_handler(
        IntentBuilder("").require("Query").require("Humidity")
        .optionally("RelativeDay").optionally("Location")
    )
    def handle_humidity(self, message):
        # Handle: How humid is it?
        intent_data = self._get_intent_data(message)
        weather = self._get_weather(intent_data)
        if weather is not None:
            intent_weather = weather.get_weather_for_intent(intent_data)
            dialog = WeatherDialog(
                intent_weather, self.weather_config, intent_data
            )
            dialog.build_humidity_dialog()
            dialog.data.update(
                humidity=self.translate(
                    "percentage.number", data=dict(num=dialog.data.humidity))
            )
            self._speak_weather(dialog)

    @intent_handler(
        IntentBuilder("").one_of("Query", "When").optionally("Location")
        .require("Sunrise")
    )
    def handle_sunrise(self, message):
        # Handle: When is the sunrise?
        intent_data = WeatherIntent(message, self.lang)
        weather = self._get_weather(intent_data)
        if weather is not None:
            intent_weather = weather.get_weather_for_intent(intent_data)
            dialog = WeatherDialog(
                intent_weather, self.weather_config, intent_data
            )
            dialog.build_sunrise_dialog()
            self._speak_weather(dialog)

    @intent_handler(
        IntentBuilder("").one_of("Query", "When").optionally("Location")
        .require("Sunset")
    )
    def handle_sunset(self, message):
        # Handle: When is the sunset?
        intent_data = WeatherIntent(message, self.lang)
        weather = self._get_weather(intent_data)
        if weather is not None:
            intent_weather = weather.get_weather_for_intent(intent_data)
            dialog = WeatherDialog(
                intent_weather, self.weather_config, intent_data
            )
            dialog.build_sunset_dialog()
            self._speak_weather(dialog)

    def _report_current_weather(self, message):
        intent_data = self._get_intent_data(message)
        weather = self._get_weather(intent_data)
        if weather is not None:
            self._display_current_conditions(weather)
            dialog = WeatherDialog(
                weather.current, self.weather_config, intent_data
            )
            dialog.build_current_weather_dialog()
            self._speak_weather(dialog)
            if self.gui.connected and self.platform != MARK_II:
                self._display_more_current_conditions(weather)
            dialog.build_high_low_temperature_dialog()
            self._speak_weather(dialog)
            if self.gui.connected:
                if self.platform == MARK_II:
                    self._display_more_current_conditions(weather)
                    sleep(5)
                    self._display_hourly_forecast(weather)
                else:
                    four_day_forecast = weather.daily[1:5]
                    self._display_forecast(four_day_forecast)

    def _display_current_conditions(self, weather):
        image_code = self.image_codes[weather.current.condition.icon]
        if self.gui.connected:
            page_name = "current_1_generic.qml"
            self.gui.clear()
            self.gui["currentTemperature"] = weather.current.temperature
            self.gui["weatherCode"] = image_code
            if self.platform == MARK_II:
                self.gui["highTemperature"] = weather.current.high_temperature
                self.gui["lowTemperature"] = weather.current.low_temperature
                page_name = page_name.replace("generic", "mark_ii")
            self.gui.show_page(page_name)
        else:
            self.enclosure.deactivate_mouth_events()
            self.enclosure.weather_display(
                image_code, weather.current.temperature
            )

    def _display_more_current_conditions(self, weather):
        page_name = "current_2_generic.qml"
        self.gui.clear()
        if self.platform == MARK_II:
            self.gui["windSpeed"] = weather.current.wind_speed
            self.gui["humidity"] = weather.current.humidity
            page_name = page_name.replace("generic", "mark_ii")
        else:
            self.gui["highTemperature"] = weather.current.high_temperature
            self.gui["lowTemperature"] = weather.current.low_temperature
        self.gui.show_page(page_name)

    def _display_hourly_forecast(self, weather):
        hourly_forecast = defaultdict(list)
        for hour_count, hourly in enumerate(weather.hourly):
            if not hour_count:
                continue
            if hour_count > 4:
                break
            # TODO: make the timeframe aware of language/location settings
            hourly_forecast['times'].append(hourly.date_time.strftime('%H:00'))
            hourly_forecast['temperatures'].append(hourly.temperature)
            hourly_forecast['weather_codes'].append(
                self.image_codes.get(hourly.condition.icon)
            )
            hourly_forecast['precipitation'].append(hourly.chance_of_precipitation)
        self.gui.clear()
        self.gui['times'] = hourly_forecast['times']
        self.gui['temperatures'] = hourly_forecast['temperatures']
        self.gui['weatherCodes'] = hourly_forecast['weather_codes']
        self.gui['chancesOfPrecipitation'] = hourly_forecast['precipitation']
        self.gui.show_page('hourly_mark_ii.qml')

    def _report_one_hour_weather(self, message):
        intent_data = self._get_intent_data(message)
        weather = self._get_weather(intent_data)
        if weather is not None:
            forecast = weather.get_forecast_for_hour(intent_data)
            dialog = WeatherDialog(
                forecast, self.weather_config, intent_data
            )
            dialog.build_hourly_weather_dialog()
            self._speak_weather(dialog)

    def _report_multi_day_forecast(self, message, days):
        intent_data = WeatherIntent(message, self.lang)
        weather = self._get_weather(intent_data)
        if weather is not None:
            forecast = weather.daily[1:days + 1]
            dialogs = self._build_forecast_dialogs(forecast, intent_data)
            self._display_forecast(forecast)
            for dialog in dialogs:
                self._speak_weather(dialog)

    def _report_one_day_forecast(self, message):
        intent_data = WeatherIntent(message, self.lang)
        weather = self._get_weather(intent_data)
        if weather is not None:
            forecast = [weather.get_forecast_for_date(intent_data)]
            dialogs = self._build_forecast_dialogs(forecast, intent_data)
            self._display_forecast(forecast)
            for dialog in dialogs:
                self._speak_weather(dialog)

    def _report_weekend_forecast(self, message):
        intent_data = self._get_intent_data(message)
        weather = self._get_weather(intent_data)
        if weather is not None:
            forecast = weather.get_weekend_forecast()
            dialogs = self._build_forecast_dialogs(forecast, intent_data)
            self._display_forecast(forecast)
            for dialog in dialogs:
                self._speak_weather(dialog)

    def _build_forecast_dialogs(self, forecast, intent_data):
        dialogs = list()
        for forecast_day in forecast:
            dialog = WeatherDialog(
                forecast_day, self.weather_config, intent_data
            )
            dialog.build_daily_weather_dialog()
            dialogs.append(dialog)

        return dialogs

    def _display_forecast(self, forecast: List):
        """Builds forecast for the upcoming days for the Mark-2 display."""
        if self.platform == MARK_II:
            self._display_forecast_mark_ii(forecast)
        else:
            self._display_forecast_generic(forecast)

    def _display_forecast_mark_ii(self, forecast):
        page_one_name = "daily_mark_ii.qml"
        display_data = defaultdict(list)
        for day in forecast:
            display_data['weatherCodes'].append(
                self.image_codes[day.condition.icon]
            )
            display_data['days'].append(day.date_time.strftime("%a"))
            display_data['highTemperatures'].append(day.temperature.high)
            display_data['lowTemperatures'].append(day.temperature.low)
        self.gui.clear()
        self.gui["numberOfDays"] = min([4, len(forecast)])
        self.gui['weatherCodes'] = display_data['weatherCodes'][:4]
        self.gui['days'] = display_data["days"][:4]
        self.gui["lowTemperatures"] = display_data["lowTemperatures"][:4]
        self.gui["highTemperatures"] = display_data["highTemperatures"][:4]
        self.gui.show_page(page_one_name)
        if len(forecast) > 4:
            sleep(20)
            self.gui.clear()
            self.gui["numberOfDays"] = min([4, len(forecast) - 4])
            self.gui['weatherCodes'] = display_data['weatherCodes'][4:]
            self.gui['days'] = display_data["days"][4:]
            self.gui["lowTemperatures"] = display_data["lowTemperatures"][4:]
            self.gui["highTemperatures"] = display_data["highTemperatures"][4:]
            self.gui.clear()
            self.gui.show_page(page_one_name)

    def _display_forecast_generic(self, forecast):
        page_one_name = "daily_1_generic.qml"
        page_two_name = page_one_name.replace("1", "2")
        display_data = []
        for day_number, day in enumerate(forecast):
            if day_number == 4:
                break
            display_data.append(
                dict(
                    weatherCode=self.image_codes[day.condition.icon],
                    highTemperature=day.temperature.high,
                    lowTemperature=day.temperature.low,
                    date=day.date_time.strftime('%a')
                )
            )
        self.gui['forecast'] = dict(
            first=display_data[:2], second=display_data[2:]
        )
        self.gui.show_page(page_one_name)
        sleep(5)
        self.gui.show_page(page_two_name)

    def _report_temperature(self, message, temperature_type=None):
        intent_data = self._get_intent_data(message)
        weather = self._get_weather(intent_data)
        if weather is not None:
            intent_weather = weather.get_weather_for_intent(intent_data)
            dialog = WeatherDialog(
                intent_weather, self.weather_config, intent_data
            )
            dialog.build_temperature_dialog(temperature_type)
            self._speak_weather(dialog)

    def _report_weather_condition(self, message, condition):
        intent_data = self._get_intent_data(message)
        weather = self._get_weather(intent_data)
        if weather is not None:
            intent_weather = weather.get_weather_for_intent(intent_data)
            dialog = self._build_condition_dialog(
                intent_weather, intent_data, condition
            )
            self._speak_weather(dialog)

    def _build_condition_dialog(self, weather, intent_data, condition):
        dialog = WeatherDialog(
            weather, self.weather_config, intent_data
        )
        intent_match = self.voc_match(weather.condition.category.lower(), condition)
        alternative_vocab = condition + 'Alternatives'
        alternative = self.voc_match(
            weather.condition.category, alternative_vocab
        )
        dialog.build_condition_dialog(condition, intent_match, alternative)
        dialog.data.update(condition=self.translate(condition))

        return dialog

    def _report_wind(self, message):
        intent_data = self._get_intent_data(message)
        weather = self._get_weather(intent_data)
        if weather is not None:
            intent_weather = weather.get_weather_for_intent(intent_data)
            intent_weather.wind_direction = self.translate(
                intent_weather.wind_direction
            )
            dialog = WeatherDialog(
                intent_weather, self.weather_config, intent_data
            )
            dialog.build_wind_dialog()
            self._speak_weather(dialog)

    def _get_intent_data(self, message):
        intent_data = None
        try:
            intent_data = WeatherIntent(message, self.lang)
        except ValueError:
            self.speak_dialog("cant.get.forecast")
        else:
            if self.voc_match(intent_data.utterance, "RelativeTime"):
                intent_data.timeframe = "hourly"
            elif self.voc_match(intent_data.utterance, "Later"):
                intent_data.timeframe = "hourly"
            elif self.voc_match(intent_data.utterance, "RelativeDay"):
                if not self.voc_match(intent_data.utterance, "Today"):
                    intent_data.timeframe = "daily"

        return intent_data

    def _get_weather(self, intent_data):
        weather = None
        if intent_data is not None:
            try:
                latitude, longitude = self._determine_weather_location(intent_data)
                weather = self.weather_api_new.get_weather_for_coordinates(
                    self.config_core.get('system_unit'), latitude, longitude
                )
            except HTTPError as api_error:
                self.log.exception("Weather API failure")
                self._handle_api_error(api_error)
            except LocationNotFoundError:
                self.log.exception("City not found.")
                self.speak_dialog(
                    "location.not.found",
                    data=dict(location=intent_data.location)
                )
            except Exception:
                self.log.exception("Unexpected error retrieving weather")
                self.speak_dialog("cant.get.forecast")

        return weather

    def _handle_api_error(self, exception):
        if exception.response.status_code == 401:
            self.bus.emit(Message("mycroft.not.paired"))
        else:
            self.speak_dialog("cant.get.forecast")

    def _determine_weather_location(self, intent_data):
        if intent_data.location is None:
            latitude = self.weather_config.latitude
            longitude = self.weather_config.longitude
        else:
            latitude = intent_data.geolocation["latitude"]
            longitude = intent_data.geolocation["longitude"]

        return latitude, longitude

    def _speak_weather(self, dialog: WeatherDialog):
        self.log.info("Speaking dialog: " + dialog.name)
        self.speak_dialog(dialog.name, dialog.data)
        mycroft.audio.wait_while_speaking()

    def _report_no_data(self, data: dict = None) -> None:
        """Do processes when Report Processes malfunction

        Arguments:
            data: Needed data for dialog on weather error processing
        """
        if data is None:
            self.speak_dialog("cant.get.forecast")
        else:
            self.speak_dialog("no.forecast", data)

    def __translate(self, condition, future=False, data=None):
        # behaviour of method dialog_renderer.render(...) has changed - instead
        # of exception when given template is not found now simply the
        # templatename is returned!?!
        if (future and
                (condition + ".future") in self.dialog_renderer.templates):
            return self.translate(condition + ".future", data)
        if condition in self.dialog_renderer.templates:
            return self.translate(condition, data)
        else:
            return condition


def create_skill():
    return WeatherSkill()
