from requests import Request, Response, Session
from bs4 import BeautifulSoup
from helenservice.client.const import HTTP_READ_TIMEOUT
import logging


class HelenSession:
    HELEN_LOGIN_HOST = "https://login.helen.fi"
    TUPAS_LOGIN_URL = "https://www.helen.fi/hcc/TupasLoginFrame?service=account&locale=fi"

    def __init__(self):
        self._session = Session()

    def login(self, username, password):
        """Login to Oma Helen web and follow redirects until the main page is reached. 
        Will save the necessary `access-token` into a Session, which can be accessed 
        with the `get_access_token()` method.

        :param username: The username for Oma Helen web service.
        :param password: The password for Oma Helen web service.
        :return: HelenSession.
        :rtype: .HelenSession
        """
        try:
            login_response = self._send_login_request(username, password)
            self._proceed_to_main_page_from_login_response(login_response)
        except Exception as e:
            logging.exception(
                "Login to Oma Helen failed. Check your credentials!")
            raise e
        return self

    def get_access_token(self):
        """Get the access-token to use the Helen API. It is required to login before the 
        token can be accessed
        """
        access_token = self._session.cookies.get("access-token")

        if access_token is None:
            raise Exception("No access token found. Log in first")

        return access_token

    def close(self):
        """Close down the session for the Oma Helen web service
        """
        self._session.close()
        logging.info("HelenSession was closed")

    def _make_url_request(self, url: str, method: str, data=None, params=None, allow_redirects: bool = False):
        request = Request(method, url)

        if data is not None:
            request.data = data
        if params is not None:
            request.params = params
        prepared_request = self._session.prepare_request(request)

        response = self._session.send(
            prepared_request, allow_redirects=allow_redirects, timeout=HTTP_READ_TIMEOUT)
        return response

    def _get_html_input_value(self, soup: BeautifulSoup, attribute_name: str):
        return soup.find("input", {"name": attribute_name}).get("value")

    def _get_html_form_url(self, soup: BeautifulSoup):
        return soup.find("form").attrs['action']

    def _get_tupas_response(self):
        return self._session.get(self.TUPAS_LOGIN_URL, timeout=HTTP_READ_TIMEOUT)

    def _send_login_request(self, username, password):
        tupas_response = self._get_tupas_response()
        tupas_soup = BeautifulSoup(tupas_response.text, "html.parser")
        authorization_url = self._get_html_form_url(tupas_soup)
        authorization_response = self._make_url_request(
            authorization_url, "POST", allow_redirects=True)
        authorization_soup = BeautifulSoup(
            authorization_response.text, "html.parser")
        login_url = self.HELEN_LOGIN_HOST + \
            self._get_html_form_url(authorization_soup)

        login_payload = {"username": username, "password": password}
        return self._make_url_request(login_url, "POST", login_payload, allow_redirects=True)

    def _proceed_to_main_page_from_login_response(self, response: Response):
        access_granted_soup = BeautifulSoup(response.text, "html.parser")
        continue_url = self._get_html_form_url(access_granted_soup)
        continue_param_code = self._get_html_input_value(
            access_granted_soup, "code")
        continue_param_state = self._get_html_input_value(
            access_granted_soup, "state")
        continue_params = {"code": continue_param_code,
                           "state": continue_param_state}
        proceed_link_page_response = self._make_url_request(
            continue_url, "GET", params=continue_params, allow_redirects=True)

        proceed_link_page_soup = BeautifulSoup(
            proceed_link_page_response.text, "html.parser")
        proceed_link_page_link_url = proceed_link_page_soup.find(
            "a").attrs['href']
        auth_response = self._make_url_request(
            proceed_link_page_link_url, "GET", allow_redirects=True)

        auth_response_soup = BeautifulSoup(auth_response.text, "html.parser")
        auth_response_url = self._get_html_form_url(auth_response_soup)
        auth_response_param_code = self._get_html_input_value(
            auth_response_soup, "code")
        auth_response_param_state = self._get_html_input_value(
            auth_response_soup, "state")
        auth_response_params = {
            "code": auth_response_param_code, "state": auth_response_param_state}

        self._make_url_request(auth_response_url, "GET", params=auth_response_params)
