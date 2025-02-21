import gzip
import io
import json
import uuid

import aiohttp
from urllib.parse import quote

from aiohttp import FormData
from fake_useragent import UserAgent
from helpers.tools import cookie_dict_to_string
from submodule_integrations.models.integration import Integration
from submodule_integrations.utils.errors import IntegrationAPIError, IntegrationAuthError


class ServiceTitanIntegration(Integration):
    def __init__(self, user_agent: str = UserAgent().random):
        super().__init__("service_titan")
        self.domain = "next.servicetitan.com"
        self.url = f"https://{self.domain}"
        self.api_url = f"{self.url}/app/api"
        self.user_agent = user_agent
        self.network_requester = None
        self.headers = None

    async def _make_request(self, method: str, url: str, **kwargs):
        if self.network_requester is not None:
            response = await self.network_requester.request(
                method, url, process_response=self._handle_response, **kwargs
            )
            return response
        else:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, **kwargs) as response:
                    return await self._handle_response(response)

    async def _handle_response(
            self, response: aiohttp.ClientResponse
    ):
        if response.status == 200:
            try:
                data = await response.json()
            except (json.decoder.JSONDecodeError, aiohttp.ContentTypeError):
                data = await response.read()

            return data

        response_json = await response.json()

        if response.status == 401:
            raise IntegrationAuthError(
                f"ServiceTitan: Auth failed",
                response.status,
            )
        elif response.status == 400:
            raise IntegrationAPIError(
                self.integration_name,
                f"{response.reason}",
                response.status,
                response.reason,
            )
        else:
            raise IntegrationAPIError(
                self.integration_name,
                f"{response_json}",
                response.status,
                response.reason,
            )

    async def initialize(self, token: dict | str, network_requester=None):
        self.headers = {
            "Host": f"{self.domain}",
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if isinstance(token, dict):
            token = cookie_dict_to_string(token)

        self.headers["Cookie"] = token
        self.network_requester = network_requester

    async def fetch_job_media(self, job_id: str):
        url = f"{self.api_url}/fam/attachments/1/{job_id}"
        params = {
            'limit': '1000',
            'photosVideosOnly': 'true',
            'includeRelatedEntities': 'false',
        }
        response = await self._make_request(method="GET", url=url, params=params, headers=self.headers)
        attachments = []
        for item in response:
            attached = {}
            file_name = item.get("filename")
            file_name = quote(file_name)
            link_url = f"{self.url}/Attach/Customer?name={file_name}"

            attached['name'] = item.get("title")
            attached['created'] = item.get("createdOn")
            attached['url'] = link_url
            attached['id'] = item.get("id")
            attachments.append(attached)

        return attachments

    async def download_image(self, link: str):
        headers = self.headers.copy()
        headers["Accept"] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        headers["Accept-Encoding"] = "gzip, deflate"

        # Use direct aiohttp request instead of _make_request to avoid JSON parsing
        async with aiohttp.ClientSession() as session:
            async with session.get(link, headers=headers) as response:
                if response.status == 200:
                    response.auto_decompress = False
                    data = {
                        "bytes": await response.read(),
                        "type": response.content_type
                    }
                    return data
                else:
                    raise IntegrationAPIError(
                        self.integration_name,
                        f"Failed to download image: {response.status}",
                        response.status,
                        response.reason
                    )

    async def upload_job_media(self, job_id: str, file_name: str, file_content: bytes, content_type: str):
        try:
            unique_id = str(uuid.uuid4())
            file_size = len(file_content)
            boundary = "----WebKitFormBoundaryCUVolhkgYrclodXz"

            # Manually construct multipart form-data
            form_data = []

            # Add all form fields
            fields = {
                'resumableChunkNumber': '1',
                'resumableChunkSize': str(file_size),
                'resumableCurrentChunkSize': str(file_size),
                'resumableTotalSize': str(file_size),
                'resumableType': content_type,
                'resumableIdentifier': unique_id,
                'resumableFilename': file_name,
                'resumableRelativePath': file_name,
                'resumableTotalChunks': '1'
            }

            # Add regular fields
            for key, value in fields.items():
                form_data.append(f'--{boundary}\r\n')
                form_data.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n')
                form_data.append(f'{value}\r\n')

            # Add file
            form_data.append(f'--{boundary}\r\n')
            form_data.append(f'Content-Disposition: form-data; name="file"; filename="blob"\r\n')
            form_data.append('Content-Type: application/octet-stream\r\n\r\n')

            # Convert form_data to bytes and combine with file content
            form_bytes = ''.join(form_data).encode('utf-8')
            final_boundary = f'\r\n--{boundary}--\r\n'.encode('utf-8')

            body = b''.join([
                form_bytes,
                file_content,
                final_boundary
            ])

            # First request - Upload the file
            headers = self.headers.copy()
            headers.update({
                'Accept': '*/*',
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': f'multipart/form-data; boundary={boundary}'
            })

            url_upload = f"{self.url}/upload/AttachmentChunkWithValidating"
            upload_response = await self._make_request(
                method="POST",
                url=url_upload,
                headers=headers,
                data=body
            )

            if not upload_response:
                raise ValueError("Failed to get response from upload endpoint")

            # Decode bytes to string if necessary
            if isinstance(upload_response, bytes):
                uploaded_name = upload_response.decode('utf-8').strip()
            else:
                uploaded_name = str(upload_response).strip()

            link = f"{self.url}/Attach/Customer?name={uploaded_name}"

            # Second request - Attach to job
            attach_data = {
                'id': int(job_id),
                'filename': uploaded_name,
                'originalFilename': file_name,
            }

            headers["Content-Type"] = "application/json; charset=UTF-8"
            url_attach = f"{self.url}/Job/AddAttachment"
            attach_response = await self._make_request(
                method="POST",
                url=url_attach,
                json=attach_data,
                headers=headers
            )

            if not attach_response:
                raise ValueError("Failed to attach file to job")

            return {
                "success": True,
                "url": link
            }

        except Exception as e:
            # Log the error details here
            raise IntegrationAPIError(
                status_code=500,
                message=f"Error uploading media: {str(e)}",
                integration_name="service_titan"
            )
