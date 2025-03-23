import logging
import requests
import shutil
import os
import re
import json
import platform
from typing import Optional, Callable
from abc import ABC, abstractmethod
from gi.repository import GLib
from urllib.parse import urlsplit

from ..lib import terminal
from ..lib.json_config import read_config_for_app
from ..lib.utils import get_random_string, url_is_valid
from ..providers.AppImageProvider import AppImageProvider, AppImageListElement
from .Models import DownloadInterruptedException

class UpdateManager(ABC):
    name = ''

    @abstractmethod
    def __init__(self, url: str, embedded=False) -> None:
        self.download_folder = GLib.get_tmp_dir() + '/it.mijorus.gearlever/downloads'
        pass

    def cleanup(self):
        pass

    @abstractmethod
    def is_update_available(self) -> bool:
        pass

    @abstractmethod
    def download(self, status_update_sb: Callable[[float], None]) -> tuple[str, str]:
        pass

    @abstractmethod
    def cancel_download(self):
        pass

    @abstractmethod
    def can_handle_link(url: str) -> bool:
        pass


class UpdateManagerChecker():
    def get_models() -> list[UpdateManager]:
        return [StaticFileUpdater, DynamicUpdater, GithubUpdater]

    def get_model_by_name(manager_label: str) -> Optional[UpdateManager]:
        item = list(filter(lambda m: m.name == manager_label, 
                                    UpdateManagerChecker.get_models()))

        if item:
            return item[0]

        return None

    def check_url_for_app(el: AppImageListElement=None):
        app_conf = read_config_for_app(el)
        update_url = app_conf.get('update_url', None)
        update_url_manager = app_conf.get('update_url_manager', None)
        return UpdateManagerChecker.check_url(update_url, el, 
            model=UpdateManagerChecker.get_model_by_name(update_url_manager))

    def check_url(url: str=Optional[str], el: Optional[AppImageListElement]=None,
                    model: Optional[UpdateManager]=None) -> Optional[UpdateManager]:

        models = UpdateManagerChecker.get_models()

        if model:
            models = list(filter(lambda m: m is model, models))

        if el:
            embedded_app_data = UpdateManagerChecker.check_app(el)

            if embedded_app_data:
                for m in models:
                    logging.debug(f'Checking embedded url with {m.__name__}')
                    if m.can_handle_link(embedded_app_data):
                        return m(embedded_app_data, embedded=True)

        if url:
            for m in models:
                logging.debug(f'Checking url with {m.__name__}')
                if m.can_handle_link(url):
                    return m(url)

        return None

    def check_app(el: AppImageListElement) -> Optional[str]:
        # if not terminal.sandbox_sh(['which', 'readelf']):
        #     return

        readelf_out = terminal.sandbox_sh(['readelf', '--string-dump=.upd_info', '--wide', el.file_path])
        readelf_out = readelf_out.replace('\n', ' ') + ' '

        # Github url
        pattern_gh = r"gh-releases-zsync\|.*(.zsync)"
        matches = re.search(pattern_gh, readelf_out)

        if matches:
            return matches[0].strip()

        # Static url
        pattern_link = r"^zsync\|http(.*)\s"
        matches = re.search(pattern_link, readelf_out)

        if matches:
            return re.sub(r"^zsync\|", '', matches[0]).strip()

        return None


class StaticFileUpdater(UpdateManager):
    label = _('Static URL')
    name = 'StaticFileUpdater'
    currend_download: Optional[requests.Response]

    def __init__(self, url, embedded=False) -> None:
        super().__init__(url)
        self.url = re.sub(r"\.zsync$", "", url)
        self.currend_download = None
        self.embedded = embedded

    def can_handle_link(url: str):
        if not url_is_valid(url):
            return False

        ct = ''

        if url.endswith('.zsync'):
            # https://github.com/AppImage/AppImageSpec/blob/master/draft.md#zsync-1
            url = re.sub(r"\.zsync$", "", url)

        headers = StaticFileUpdater.get_url_headers(url)
        ct = headers.get('content-type', '')

        logging.debug(f'{url} responded with content-type: {ct}')
        ct_supported = ct in [*AppImageProvider.supported_mimes, 'binary/octet-stream', 'application/octet-stream']

        if not ct_supported:
            logging.warn(f'Provided url "{url}" does not return a valid content-type header')

        return ct_supported

    def download(self, status_update_cb) -> str:
        logging.info(f'Downloading file from {self.url}')

        self.currend_download = requests.get(self.url, stream=True)
        random_name = get_random_string()
        fname = f'{self.download_folder}/{random_name}.appimage'

        if not os.path.exists(self.download_folder):
            os.makedirs(self.download_folder)

        etag = self.currend_download.headers.get("etag", '')
        total_size = int(self.currend_download.headers.get("content-length", 0))
        status = 0
        block_size = 1024

        if os.path.exists(fname):
            os.remove(fname)

        with open(fname, 'wb') as f:
            for chunk in self.currend_download.iter_content(block_size):
                f.write(chunk)

                status += block_size

                if total_size:
                    status_update_cb(status / total_size)

        if os.path.getsize(fname) < total_size:
            raise DownloadInterruptedException()

        self.currend_download = None
        return fname, etag

    def cancel_download(self):
        if self.currend_download:
            self.currend_download.close()
            self.currend_download = None

    def cleanup(self):
        if os.path.exists(self.download_folder):
            shutil.rmtree(self.download_folder)

    def is_update_available(self, el: AppImageListElement):
        headers = StaticFileUpdater.get_url_headers(self.url)
        resp_cl = int(headers.get('content-length', '0'))
        old_size = os.path.getsize(el.file_path)

        logging.debug(f'StaticFileUpdater: new url has length {resp_cl}, old was {old_size}')

        if resp_cl == 0:
            return False

        is_size_different = resp_cl != old_size
        return is_size_different
    
    def get_url_headers(url):
        headers = {}
        head_request_error = False

        try:
            resp = requests.head(url, allow_redirects=True)
            resp.raise_for_status()
            headers = resp.headers
        except Exception as e:
            head_request_error = True
            logging.error(str(e))
            
        if head_request_error:
            # If something goes wrong with the Head request, try with stream mode
            logging.warn('Head request failed, trying with stream mode...')

            try:
                resp = requests.get(url, allow_redirects=True, stream=True)
                with resp as r:
                    r.raise_for_status()
                    headers = r.headers
                    r.close()
            except Exception as e:
                logging.error(str(e))
        
        return headers

class DynamicUpdater(StaticFileUpdater):
    label = _('Dynamic URL')
    name = 'DynamicUpdater'

    def __init__(self, url, embedded=False) -> None:
        self.original_url = url
        self.embedded = embedded
        # Resolve dynamic URL to static URL
        static_url = self.get_static_url(url)
        # Initialize the parent class with the resolved static URL
        super().__init__(static_url, embedded) if static_url else super().__init__(url, embedded)
        # Keep track of both URLs
        self.dynamic_url = url

    def get_static_url(self, url) -> str:
        static_url = ''
        headers = {
            'User-Agent': DynamicUpdater.get_user_agent(),
            'Accept': 'application/octet-stream,application/x-appimage,*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': url,
        }
        try:
            logging.debug(f"Attempting to resolve dynamic URL: {url}")
            resp = requests.get(url, allow_redirects=True, headers=headers)
            resp.raise_for_status()

            logging.debug(f"Response status: {resp.status_code}")
            logging.debug(f"Response headers: {resp.headers}")

            # First check for Location header (redirect)
            static_url = resp.headers.get("location")

            # If no location header, check if we were redirected
            if not static_url and resp.url != url:
                static_url = resp.url
                logging.debug(f"Using final URL after redirect: {static_url}")

            # If still no URL, check Content-Disposition header
            if not static_url:
                content_disp = resp.headers.get("content-disposition", "")
                if "filename=" in content_disp:
                    # This is likely a direct download link
                    static_url = url
                    logging.debug(f"Using original URL as it has Content-Disposition: {content_disp}")

            if not static_url:
                logging.error(f"Could not extract download URL from response")
            else:
                logging.debug(f"Resolved to static URL: {static_url}")

        except requests.exceptions.RequestException as e:
            logging.error(f"Request failed: {str(e)}")
        except Exception as e:
            logging.error(f"An unexpected error occurred: {str(e)}")
        return static_url

    @staticmethod
    def can_handle_link(url: str) -> bool:
        try:
            # First check if URL contains obvious AppImage indicators
            if '.appimage' in url.lower() or 'appimage' in url.lower():
                logging.debug(f"URL appears to be an AppImage link: {url}")
                return True

            # Check with HEAD request
            headers = {
                'User-Agent': DynamicUpdater.get_user_agent(),
                'Accept': 'application/octet-stream,application/x-appimage,*/*',
            }

            try:
                resp = requests.head(url, allow_redirects=True, headers=headers, timeout=5)

                # Check Content-Type
                content_type = resp.headers.get('content-type', '').lower()
                if content_type in ['application/octet-stream', 'application/x-appimage', 'binary/octet-stream']:
                    logging.debug(f"Content-Type suggests AppImage: {content_type}")
                    return True

                # Check Content-Disposition
                content_disp = resp.headers.get('content-disposition', '').lower()
                if 'appimage' in content_disp:
                    logging.debug(f"Content-Disposition suggests AppImage: {content_disp}")
                    return True

                # Don't reject yet - some download pages don't reveal file type in headers
            except Exception as e:
                logging.debug(f"HEAD request failed, trying fallback detection: {str(e)}")

            # Fallback: look for common download page patterns
            common_download_domains = [
                'download.', '.releases.', 'releases.', 'github.com',
                'gitlab.', 'sourceforge.net', 'dl.', '.io/download'
            ]

            for domain in common_download_domains:
                if domain in url.lower():
                    logging.debug(f"URL appears to be a download page: {url}")
                    return True

            logging.debug(f"URL doesn't appear to be an AppImage download: {url}")
            return False

        except Exception as e:
            logging.error(f"Error checking if can handle link: {str(e)}")
            return False

    def download(self, status_update_cb) -> tuple[str, str]:
        # If URL failed to resolve initially, try one more time
        if not self.url or self.url == self.original_url:
            static_url = self.get_static_url(self.original_url)
            if static_url:
                self.url = static_url
                logging.debug(f"Resolved URL before download: {static_url}")
        
        # Use parent class's download method
        return super().download(status_update_cb)

    def is_update_available(self, el: AppImageListElement) -> bool:
        # Always refresh the static URL when checking for updates
        fresh_static_url = self.get_static_url(self.original_url)
        
        # If URL has changed, update is available
        if fresh_static_url and fresh_static_url != self.url:
            logging.debug(f"Static URL has changed: {self.url} -> {fresh_static_url}")
            self.url = fresh_static_url
            return True
            
        # Otherwise use parent method to check
        return super().is_update_available(el)

    @staticmethod
    def get_url_headers(url):
        url_headers = StaticFileUpdater.get_url_headers(url)
        url_headers["User-Agent"] = DynamicUpdater.get_user_agent()
        url_headers["Accept"] = "application/octet-stream,application/x-appimage,*/*"
        url_headers["Accept-Language"] = "en-US,en;q=0.9"
        return url_headers

    @staticmethod
    def get_user_agent():
        arch = platform.machine()
        # More realistic, modern browser user agent
        return f"Mozilla/5.0 (X11; Linux {arch}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"

class GithubUpdater(UpdateManager):
    staticfile_manager: Optional[StaticFileUpdater]
    label = 'Github'
    name = 'GithubUpdater'

    def __init__(self, url, embedded=False) -> None:
        super().__init__(url)
        self.staticfile_manager = None
        self.url_data = GithubUpdater.get_url_data(url)

        self.url = f'https://github.com/{self.url_data["username"]}/{self.url_data["repo"]}'
        self.url += f'/releases/download/{self.url_data["tag_name"]}/{self.url_data["filename"]}'

        self.embedded = embedded

    def get_url_data(url: str):
        # Format gh-releases-zsync|probono|AppImages|latest|Subsurface-*x86_64.AppImage.zsync
        # https://github.com/AppImage/AppImageSpec/blob/master/draft.md#github-releases

        tag_name = '*'
        if url.startswith('https://'):
            logging.debug(f'GithubUpdater: found http url, trying to detect github data')
            urldata = urlsplit(url)

            if urldata.netloc != 'github.com':
                return False

            paths = urldata.path.split('/')

            if len(paths) != 7:
                return False

            if paths[3] != 'releases' or paths[4] != 'download':
                return False

            rel_name = 'latest'
            tag_name = paths[5]

            url = f'|{paths[1]}|{paths[2]}|{rel_name}|{paths[6]}'
            logging.debug(f'GithubUpdater: generated appimages-like update string "{url}"')

        items = url.split('|')

        if len(items) != 5:
            return False

        return {
            'username': items[1],
            'repo': items[2],
            'release': items[3],
            'filename': items[4],
            'tag_name': tag_name
        }

    def can_handle_link(url: str):
        return GithubUpdater.get_url_data(url) != False

    def download(self, status_update_cb) -> str:
        target_asset = self.fetch_target_asset()
        if not target_asset:
            logging.warn('Missing target_asset for GithubUpdater instance')
            return

        dwnl = target_asset['browser_download_url']
        self.staticfile_manager = StaticFileUpdater(dwnl)
        fname, etag = self.staticfile_manager.download(status_update_cb)

        self.staticfile_manager = None
        return fname, target_asset['id']

    def cancel_download(self):
        if self.staticfile_manager:
            self.staticfile_manager.cancel_download()
            self.staticfile_manager = None

    def cleanup(self):
        if self.staticfile_manager:
            self.staticfile_manager.cleanup()

    def convert_glob_to_regex(self, glob_str):
        """
        Converts a string with glob patterns to a regular expression.

        Args:
            glob_str: A string containing glob patterns.

        Returns:
            A regular expression string equivalent to the glob patterns.
        """
        regex = ""
        for char in glob_str:
            if char == "*":
                regex += r".*"
            else:
                regex += re.escape(char)

        regex = f'^{regex}$'
        return regex

    def fetch_target_asset(self):
        rel_url = f'https://api.github.com/repos/{self.url_data["username"]}/{self.url_data["repo"]}'
        rel_url += f'/releases/{self.url_data["release"]}'

        try:
            rel_data_resp = requests.get(rel_url)
            rel_data_resp.raise_for_status()
            rel_data = rel_data_resp.json()
        except Exception as e:
            logging.error(e)
            return

        logging.debug(f'Found {len(rel_data["assets"])} assets from {rel_url}')

        zsync_file = None
        target_re = re.compile(self.convert_glob_to_regex(self.url_data['filename']))
        target_tag = re.compile(self.convert_glob_to_regex(self.url_data['tag_name']))

        if not re.match(target_tag, rel_data['tag_name']):
            logging.debug(f'Release tag names do not match: {rel_data["tag_name"]} != {self.url_data["tag_name"]}')
            return

        possible_targets = []
        for asset in rel_data['assets']:
            if self.embedded:
                if re.match(target_re, asset['name']) and asset['name'].endswith('.zsync'):
                    possible_targets = [asset]
                    break
            else:
                if re.match(target_re, asset['name']):
                    possible_targets.append(asset)

        if len(possible_targets) == 1:
            zsync_file = possible_targets[0]
        else:
            logging.info(f'found {len(possible_targets)} possible file targets')
            
            for t in possible_targets:
                logging.info(' - ' + t['name'])

            # Check possible differences with system architecture in file name
            system_arch = terminal.sandbox_sh(['arch'])
            is_x86 = re.compile(r'(\-|\_|\.)x86(\-|\_|\.)')
            is_arm = re.compile(r'(\-|\_|\.)(arm64|aarch64|armv7l)(\-|\_|\.)')

            if system_arch == 'x86_64':
                for t in possible_targets:
                    if is_x86.search(t['name']) or not is_arm.search(t['name']):
                        zsync_file = t
                        logging.info('found possible target: ' + t['name'])
                        break

        if not zsync_file:
            logging.debug(f'No matching assets found from {rel_url}')
            return

        target_file = re.sub(r'\.zsync$', '', zsync_file['name'])

        for asset in rel_data['assets']:
            if asset['name'] == target_file:
                logging.debug(f'Found 1 matching asset: {asset["name"]}')
                return asset

    def is_update_available(self, el: AppImageListElement):
        target_asset = self.fetch_target_asset()

        if target_asset:
            ct_supported = target_asset['content_type'] in [*AppImageProvider.supported_mimes, 'raw',
                                                    'binary/octet-stream', 'application/octet-stream']

            if ct_supported:
                old_size = os.path.getsize(el.file_path)
                is_size_different = target_asset['size'] != old_size
                return is_size_different

        return False



