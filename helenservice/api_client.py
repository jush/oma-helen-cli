from cachetools import cached, TTLCache
import json, logging
from datetime import date, datetime, timedelta

from helenservice.api_exceptions import InvalidApiResponseException
from .api_response import MeasurementResponse, SpotPricesResponse
from .const import HTTP_READ_TIMEOUT
from .helen_session import HelenSession
from requests import get
from dateutil.relativedelta import relativedelta


# TODO: consider moving all calculation functions somewhere else - they are not related to HelenApiClient
class HelenApiClient:
    HELEN_API_URL_V14 = "https://api.omahelen.fi/v14"
    MEASUREMENTS_ENDPOINT = "/measurements/electricity"
    TRANSFER_ENDPOINT = "/measurements/electricity-transfer"
    SPOT_PRICES_ENDPOINT = MEASUREMENTS_ENDPOINT + "/spot-prices"
    CONTRACT_ENDPOINT = "/contract/list"

    _latest_login_time: datetime = None
    _session: HelenSession = None  
    _margin: float = None

    def __init__(self, tax: float = None, margin: float = None):
        self._tax = 0.24 if tax is None else tax
        self._margin = 0.38 if margin is None else margin

    def login(self, username, password):
        """Login to Oma Helen. Creates a new session when called."""
        self._session = HelenSession().login(username, password)
        self._latest_login_time = datetime.now()
        return self

    def is_session_valid(self):
        """If the latest login has happened within the last hour, then the session should be valid and ready to go"""
        if self._latest_login_time is None:
            return False
        now = datetime.now()
        is_latest_login_within_hour = now-timedelta(hours=1) <= self._latest_login_time <= now
        return is_latest_login_within_hour

    def close(self):
        if self._session is not None:
            self._session.close()

    def _get_hourly_consumption_costs(self, start_date: date, end_date: date, delivery_site_id: int = None) -> list:
        hourly_prices_response = self.get_hourly_spot_prices_between_dates(start_date, end_date, delivery_site_id)
        if not hourly_prices_response.interval: return []
        # retain the hourly-price/hourly-measurement pairs (list ordering matters!) by assigning invalid items None
        hourly_prices = list(map(lambda price: price if price.status == 'valid' else None, hourly_prices_response.interval.measurements))
        hourly_measurements_data = self.get_hourly_measurements_between_dates(start_date, end_date, delivery_site_id).intervals.electricity
        if not hourly_measurements_data: return []
        hourly_measurements = list(map(lambda measurement: measurement if measurement.status == 'valid' else None, hourly_measurements_data[0].measurements))
        length = min(hourly_prices.__len__(), hourly_measurements.__len__())
        if length == 0: return []
        hourly_consumption_costs = []
        for i in range(length):
            hourly_price = hourly_prices[i]
            hourly_measurement = hourly_measurements[i]
            if hourly_price is None or hourly_measurement is None: continue
            hourly_price_with_tax_and_margin = hourly_price.value+self._margin
            hourly_consumption_costs.append((abs(hourly_price_with_tax_and_margin*hourly_measurement.value)))
        return hourly_consumption_costs

    def calculate_transfer_fees_between_dates(self, start_date: date, end_date: date, delivery_site_id: int = None):
        """Calculate your total transfer fee costs including the monthly base price.
        An optional delivery site ID can be provided to limit the data to a specific site. If none is provided, the
        latest active contract will be used.

        Returns the price in euros
        """
        total_consumption = self.get_total_consumption_between_dates(start_date, end_date, delivery_site_id)
        transfer_fee = self.get_transfer_fee(delivery_site_id)
        total_price = total_consumption * transfer_fee
        total_price_in_euros = total_price/100 + self.get_transfer_base_price(delivery_site_id)
        return total_price_in_euros
    
    def get_total_consumption_between_dates(self, start_date: date, end_date: date, delivery_site_id: int = None) -> float:
        daily_measurements_response = self.get_daily_measurements_between_dates(start_date, end_date, delivery_site_id)
        daily_measurements = list(filter(lambda measurement: measurement.status == 'valid', daily_measurements_response.intervals.electricity[0].measurements))
        total_consumption = sum(map(lambda measurement: abs(measurement.value), daily_measurements))
        return total_consumption

    def calculate_total_costs_by_spot_prices_between_dates(self, start_date: date, end_date: date, delivery_site_id: int = None):
        """Calculate your total electricity cost with according spot prices by hourly precision
        An optional delivery site ID can be provided to limit the data to a specific site. If none is provided, the
        latest active contract will be used.

        Returns the price in euros
        """
        hourly_consumption_costs = self._get_hourly_consumption_costs(start_date, end_date, delivery_site_id)
        total_price = sum(hourly_consumption_costs)
        total_price_with_tax_in_euros = total_price*(1+self._tax)/100
        return total_price_with_tax_in_euros

    def calculate_impact_of_usage_between_dates(self, start_date: date, end_date: date, delivery_site_id: int = None) -> float:
        """Calculate the price impact of your usage based on hourly consumption and hourly spot prices

        The price impact increases or decreases your contract's unit price in certain contracts
        such as the Helen Smart Electricity Guarantee contract
        https://www.helen.fi/en/electricity/electricity-products-and-prices/smart-electricity-guarantee
        A negative number decreases and a positive number increases the base price.

        According to Helen, the impact is calculated with formula (A-B) / E = c/kWh, where
        A = the sum of hourly consumption multiplied with the hourly price (i.e. your weighted average price of each hour)
        B = total consumption multiplied with the whole month's average market price (i.e. your average price of the whole month)
        E = total consumption
        """
        hourly_prices_response = self.get_hourly_spot_prices_between_dates(start_date, end_date, delivery_site_id)
        if not hourly_prices_response.interval: return 0.0
        # retain the hourly-price/hourly-measurement pairs (list ordering matters!) by assigning invalid items None
        hourly_prices = list(map(lambda price: price if price.status == 'valid' else None, hourly_prices_response.interval.measurements))
        hourly_measurements_response = self.get_hourly_measurements_between_dates(start_date, end_date, delivery_site_id)
        if not hourly_measurements_response.intervals or not hourly_measurements_response.intervals.electricity: return 0.0
        hourly_measurements = list(map(lambda measurement: measurement if measurement.status == 'valid' else None, hourly_measurements_response.intervals.electricity[0].measurements))
        length = min(hourly_prices.__len__(), hourly_measurements.__len__())
        if length == 0: return 0.0
        hourly_prices_without_nones = list(filter(lambda price: price is not None, hourly_prices))
        hourly_measurements_without_nones = list(filter(lambda measurement: measurement is not None, hourly_measurements))
        hourly_weighted_consumption_prices = []
        for i in range(length):
            hourly_price = hourly_prices[i]
            hourly_measurement = hourly_measurements[i]
            if hourly_price is None or hourly_measurement is None: continue
            hourly_weighted_consumption_prices.append((abs(hourly_price.value*hourly_measurement.value)))
        if not hourly_weighted_consumption_prices: return 0.0
        monthly_average_price = sum(map(lambda price: abs(price.value), hourly_prices_without_nones))/hourly_prices_without_nones.__len__()
        total_consumption = sum(map(lambda measurement: abs(measurement.value), hourly_measurements_without_nones))
        total_hourly_weighted_consumption_prices = sum(hourly_weighted_consumption_prices)
        total_consumption_average_price = monthly_average_price*total_consumption
    
        impact = (total_hourly_weighted_consumption_prices-total_consumption_average_price)/total_consumption
    
        return impact
            
    @cached(cache=TTLCache(maxsize=4, ttl=3600))
    def get_daily_measurements_between_dates(self, start: date, end: date, delivery_site_id: int = None) -> MeasurementResponse:
        """Get electricity measurements for each day of the wanted month of the on-going year.
        An optional delivery site ID can be provided to limit the data to a specific site. If none is provided, the
        latest active contract will be used.
        """

        previous_day = start + relativedelta(days=-1)
        start_time = f"{previous_day}T22:00:00+00:00"
        end_time = f"{end}T21:59:59+00:00"
        measurements_url, delivery_site_id = self._get_measurements_endpoint_and_delivery_site_id(delivery_site_id)
        measurements_params = {
            "begin": start_time,
            "end": end_time,
            "resolution": "day",
            "delivery_site_id": delivery_site_id,
            "allow_transfer": "true"
        }

        response_json_text = get(
            measurements_url, measurements_params, headers=self._api_request_headers(), timeout=HTTP_READ_TIMEOUT).text
        daily_measurement: MeasurementResponse = MeasurementResponse(
            **json.loads(response_json_text))

        return daily_measurement

    @cached(cache=TTLCache(maxsize=2, ttl=3600))
    def get_monthly_measurements_by_year(self, year: int, delivery_site_id: int = None) -> MeasurementResponse:
        """Get electricity measurements for each month of the selected year.
        An optional delivery site ID can be provided to limit the data to a specific site. If none is provided, the
        latest active contract will be used.
        """
        
        last_year = year-1
        start_time = f"{last_year}-12-31T22:00:00+00:00"
        end_time = f"{year}-12-31T21:59:59+00:00"
        measurements_url, delivery_site_id = self._get_measurements_endpoint_and_delivery_site_id(delivery_site_id)
        measurements_params = {
            "begin": start_time,
            "end": end_time,
            "resolution": "month",
            "delivery_site_id": delivery_site_id,
            "allow_transfer": "true"
        }

        response_json_text = get(
            measurements_url, measurements_params, headers=self._api_request_headers(), timeout=HTTP_READ_TIMEOUT).text
        monthly_measurement: MeasurementResponse = MeasurementResponse(
            **json.loads(response_json_text))

        return monthly_measurement


    @cached(cache=TTLCache(maxsize=4, ttl=3600))
    def get_hourly_measurements_between_dates(self, start: date, end: date, delivery_site_id: int = None) -> MeasurementResponse:
        """Get electricity measurements for each hour between given dates.
        An optional delivery site ID can be provided to limit the data to a specific site. If none is provided, the
        latest active contract will be used.
        """
        
        previous_day = start + relativedelta(days=-1)
        start_time = f"{previous_day}T22:00:00+00:00"
        end_time = f"{end}T21:59:59+00:00"
        measurements_url, delivery_site_id = self._get_measurements_endpoint_and_delivery_site_id(delivery_site_id)
        measurements_params = {
            "begin": start_time,
            "end": end_time,
            "resolution": "hour",
            "delivery_site_id": delivery_site_id,
            "allow_transfer": "true"
        }

        response_json_text = get(
            measurements_url, measurements_params, headers=self._api_request_headers(), timeout=HTTP_READ_TIMEOUT).text
        hourly_measurement: MeasurementResponse = MeasurementResponse(
            **json.loads(response_json_text))

        return hourly_measurement


    @cached(cache=TTLCache(maxsize=4, ttl=3600))
    def get_hourly_spot_prices_between_dates(self, start: date, end: date, delivery_site_id: int = None) -> SpotPricesResponse:
        """Get electricity spot prices for each hour between given dates.
        An optional delivery site ID can be provided to limit the data to a specific site. If none is provided, the
        latest active contract will be used.
        """
        
        previous_day = start + relativedelta(days=-1)
        start_time = f"{previous_day}T22:00:00+00:00"
        end_time = f"{end}T21:59:59+00:00"
        
        if delivery_site_id == None:
            delivery_site_id = self._get_contract_by_delivery_site_id()["delivery_site"]["id"]
        
        spot_prices_params = {
            "begin": start_time,
            "end": end_time,
            "resolution": "hour",
            "delivery_site_id": delivery_site_id,
            "allow_transfer": "true"
        }

        spot_prices_url = self.HELEN_API_URL_V14 + self.SPOT_PRICES_ENDPOINT
        response_json_text = get(
            spot_prices_url, spot_prices_params, headers=self._api_request_headers(), timeout=HTTP_READ_TIMEOUT).text
        spot_prices_measurement: SpotPricesResponse = SpotPricesResponse(
            **json.loads(response_json_text))

        return spot_prices_measurement
    
    @cached(cache=TTLCache(maxsize=2, ttl=3600))
    def get_contract_data_json(self):
        """Get your contract data."""

        contract_url = self.HELEN_API_URL_V14 + self.CONTRACT_ENDPOINT
        contract_params = {
            "include_transfer": "true",
            "update": "true",
            "include_products": "true"
        }
        contract_response_dict = get(contract_url, headers=self._api_request_headers(), timeout=HTTP_READ_TIMEOUT, params=contract_params).json()
        contracts_dict = contract_response_dict["contracts"]
        self._contract_data_dict = contracts_dict
        return contracts_dict

    def get_latest_active_delivery_site_id(self) -> int:
        """Get the delivery site id from your latest active contract."""

        return self._get_contract_by_delivery_site_id()["delivery_site"]["id"]

    def get_contract_base_price(self, delivery_site_id: int = None) -> float:
        """Get the contract base price from your contract data.
        An optional delivery site ID can be provided to limit the data to a specific site. If none is provided, the
        latest active contract will be used.
        """

        contract = self._get_contract_by_delivery_site_id(delivery_site_id)

        if not contract: raise InvalidApiResponseException("Contract data is empty or None")
        products = contract["products"] if contract else []
        product = next(filter(lambda p: p["product_type"] == "energy", products), None) 
        if not product: 
            logging.warn("Could not resolve contract base price from Helen API response. Returning 0.0")
            return 0.0
        components = product["components"] if product else []
        base_price_component = next(filter(lambda component: component["is_base_price"], components), None)
        if not base_price_component: 
            logging.warn("Could not resolve contract base price from Helen API response. Returning 0.0")
            return 0.0
        return base_price_component["price"]
    
    def get_contract_energy_unit_price(self, delivery_site_id: int = None) -> float:
        """
        Get the fixed unit price for electricity from your contract data. Returns '0.0' for spot electricity contracts
        because the price is not fixed in your contract when using spot.
        An optional delivery site ID can be provided to limit the data to a specific site. If none is provided, the
        latest active contract will be used.
        """
        
        contract = self._get_contract_by_delivery_site_id(delivery_site_id)
        if not contract: raise InvalidApiResponseException("Contract data is empty or None")
        products = contract["products"] if contract else []
        product = next(filter(lambda p: p["product_type"] == "energy", products), None) 
        if not product: 
            logging.warn("Could not resolve energy price from Helen API response. Returning 0.0")
            return 0.0
        if not product: raise InvalidApiResponseException("Product data is empty or None")
        components = product["components"] if product else []
        energy_unit_price_component = next(filter(lambda component: component["name"] == "Energia", components), None)
        if not energy_unit_price_component: 
            logging.warn("Could not resolve energy price from Helen API response. Returning 0.0")
            return 0.0
        return energy_unit_price_component["price"]

    def get_transfer_fee(self, delivery_site_id: int = None) -> float:
        """Get the transfer fee price (c/kWh) from your contract data. Returns '0.0' if Helen is not your transfer company
        An optional delivery site ID can be provided to limit the data to a specific site. If none is provided, the
        latest active contract will be used.
        """

        contract = self._get_contract_by_delivery_site_id(delivery_site_id)
        if not contract: raise InvalidApiResponseException("Contract data is empty or None")
        products = contract["products"] if contract else []
        product = next(filter(lambda p: p["product_type"] == "transfer", products), None) 
        if not product: 
            logging.warn("Could not resolve transfer fees from Helen API response. Returning 0.0")
            return 0.0
        components = product["components"] if product else []
        transfer_fee_component = next(filter(lambda component: component["name"] == "Siirtomaksu", components), None)
        if transfer_fee_component is None: 
            logging.warn("Could not resolve transfer fees from Helen API response. Returning 0.0")
            return 0.0
        return transfer_fee_component["price"]
    
    def get_transfer_base_price(self, delivery_site_id: int = None) -> float:
        """Get the transfer base price (eur) from your contract data. Returns '0.0' if Helen is not your transfer company"""

        contract = self._get_contract_by_delivery_site_id(delivery_site_id)
        if not contract: raise InvalidApiResponseException("Contract data is empty or None")
        products = contract["products"] if contract else []
        product = next(filter(lambda p: p["product_type"] == "transfer", products), None) 
        if not product: 
            logging.warn("Could not resolve transfer base price from Helen API response. Returning 0.0")
            return 0.0
        components = product["components"] if product else []
        transfer_base_price_component = next(filter(lambda component: component["is_base_price"], components), None)
        if transfer_base_price_component is None: 
            logging.warn("Could not resolve transfer base price from Helen API response. Returning 0.0")
            return 0.0
        return transfer_base_price_component["price"]

    def get_api_access_token(self):
        return self._session.get_access_token()

    def _api_request_headers(self):
        return {
            "Authorization": f"Bearer {self.get_api_access_token()}",
            "Accept": "application/json"
        }

    def set_margin(self, margin: float):
        self._margin = margin

    def _get_latest_active_contract(self, contracts):
        """
        Resolves an active contract from a list of contracts. If there are multiple active contracts, will
        return the newest one.
        """
        active_contracts = self.filter_active_contracts(contracts)
        if active_contracts.__len__() > 1:
            logging.warn("Found multiple active Helen contracts. Using the newest one.")
            active_contracts.sort(key=lambda contract: datetime.strptime(contract["start_date"], '%Y-%m-%dT%H:%M:%S'), reverse=True)
        if active_contracts.__len__() == 0:
            logging.error("No active contracts found")
            return None
        return active_contracts[0]
    
    def filter_active_contracts(self, contracts):
        """Resolves all active contracts from a list of contracts."""

        active_contracts = list(filter(lambda contract: contract["end_date"] == None or self._date_is_now_or_later(contract["end_date"]), contracts))
        active_contracts = list(filter(lambda contract: contract["domain"] != "electricity-production", active_contracts))
        return active_contracts
    
    def _date_is_now_or_later(self, end_date_str):
        end_date = datetime.strptime(end_date_str, '%Y-%m-%dT%H:%M:%S')
        now = datetime.now()
        return end_date >= now
    
    def _get_measurements_endpoint_and_delivery_site_id(self, delivery_site_id: int = None):
        """Resolves the right URL to get the measurements for the given delivery site ID.
        If no delivery site ID is provided, the site ID from the latest active contract will be used.
        """

        contract = self._get_contract_by_delivery_site_id(delivery_site_id)

        if 'domain' in contract and contract['domain'] == 'electricity-transfer':
            return self.HELEN_API_URL_V14 + self.TRANSFER_ENDPOINT, contract["delivery_site"]["id"]
        return self.HELEN_API_URL_V14 + self.MEASUREMENTS_ENDPOINT, contract["delivery_site"]["id"]

    def _get_contract_by_delivery_site_id(self, delivery_site_id: int = None):
        """Returns the contract linked to the given delivery site ID. If no delivery site ID is given,
        it returns the latest active contract.
        """

        contracts = self.get_contract_data_json()
        if delivery_site_id == None:
            return self._get_latest_active_contract(contracts)
        else:
            # Try to find the delivery site ID from the list of active contracts
            active_contracts = self.filter_active_contracts(contracts)
            active_contracts = list(filter(lambda contract: contract["delivery_site"]["id"] == delivery_site_id, active_contracts))
            if active_contracts.__len__() == 0:
                return active_contracts[0]
            elif active_contracts.__len__() > 1:
                logging.warn("Found multiple active Helen contracts matching the same delivery site ID. Using the newest one.")
                active_contracts.sort(key=lambda contract: datetime.strptime(contract["start_date"], '%Y-%m-%dT%H:%M:%S'), reverse=True)
                return active_contracts[0]
            else:
                logging.warn("No active Helen contracts matching the delivery site ID found. Searching in non-active contracts.")
                # If none found, then search also from non-active ones
                return list(filter(lambda contract: contract["delivery_site"]["id"] == delivery_site_id, contracts))[0]