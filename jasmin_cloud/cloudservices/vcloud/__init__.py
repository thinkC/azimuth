"""
This module defines an implementation of the cloud services interfaces for the
`VMWare vCloud Director 5.5 <http://pubs.vmware.com/vcd-55/index.jsp>`_ API.
"""

__author__ = "Matt Pryor"
__copyright__ = "Copyright 2015 UK Science and Technology Facilities Council"


import os, uuid, re, logging
from ipaddress import IPv4Address, AddressValueError, summarize_address_range
from time import sleep
from datetime import datetime
import xml.etree.ElementTree as ET

import requests
from jinja2 import Environment, FileSystemLoader

from .. import NATPolicy, MachineStatus, Image, HardDisk, Machine, Session
from ..exceptions import *


# Prefixes for vCD namespaces
_NS = {
    'vcd'  : 'http://www.vmware.com/vcloud/v1.5',
    'xsi'  : 'http://www.w3.org/2001/XMLSchema-instance',
    'ovf'  : 'http://schemas.dmtf.org/ovf/envelope/1',
    'rasd' : 'http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData',
}

# Required headers for all requests
_REQUIRED_HEADERS = { 'Accept':'application/*+xml;version=5.5' }

# Map of vCD vApp status codes to MachineStatus instances
_STATUS_MAP = {
    -1 : MachineStatus.PROVISIONING_FAILED,
     0 : MachineStatus.PROVISIONING,
     1 : MachineStatus.UNKNOWN,
     3 : MachineStatus.SUSPENDED,
     4 : MachineStatus.POWERED_ON,
     5 : MachineStatus.WAITING_FOR_INPUT,
     6 : MachineStatus.UNKNOWN,
     7 : MachineStatus.UNRECOGNISED,
     8 : MachineStatus.POWERED_OFF,
     9 : MachineStatus.INCONSISTENT,
    10 : MachineStatus.UNKNOWN,
}

# Poll interval for checking tasks
_POLL_INTERVAL = 2

# Jinja2 environment for loading XML templates from the same directory as this
# script is in
_ENV = Environment(
    loader = FileSystemLoader(os.path.dirname(os.path.realpath(__file__)))
)

# Logger
_log = logging.getLogger(__name__)


###############################################################################
###############################################################################


class VCloudError(ProviderSpecificError):
    """
    Provider specific error class for the vCloud Director provider.

    .. py:attribute:: __endpoint__

        The API endpoint that raised the error.

    .. py:attribute:: __user__

        The user when the error was raised.

    .. py:attribute:: __status_code__

        The majorErrorCode from the vCD error - always matches the HTTP status
        code of the response.

    .. py:attribute:: __error_code__

        The minorErrorCode from the vCD error.
    """
    def __init__(self, endpoint, user, status_code, error_code, error_message):
        self.__endpoint__    = endpoint
        self.__user__        = user
        self.__status_code__ = status_code
        self.__error_code__  = error_code
        super().__init__(error_message)

    def __str__(self):
        return "[{}] [{}] [{}] [{}] {}".format(
            self.__endpoint__, self.__user__,
            self.__status_code__, self.__error_code__, super().__str__()
        )

    def raise_cloud_service_error(self):
        """
        Raises the appropriate :py:class`..exceptions.CloudServiceError` with this
        vCD error as the cause.
        """
        # A 503 error probably means we couldn't even contact vCD
        if self.__status_code__ == 503:
            raise ProviderConnectionError('Cannot connect to vCloud Director API') from self
        # Any other 5xx error indicates a problem on the server
        if self.__status_code__ >= 500:
            raise ProviderUnavailableError('vCloud Director API encountered an error') from self
        # 4xx errors indicate that there was a problem with our request, so we want
        # to try and be more specific
        # For the status codes returned by the vCD API, see
        # http://pubs.vmware.com/vcd-55/topic/com.vmware.vcloud.api.doc_55/GUID-D2B2E6D4-7A92-4D1B-80C0-F32AE0CA3D11.html
        if self.__status_code__ == 401:
            # 401 is reported if authentication failed
            raise AuthenticationError('Authentication failed') from self
        elif self.__status_code__ == 403:
            # 403 is reported if the authenticated user doesn't have adequate
            # permissions
            raise PermissionsError('Insufficient permissions') from self
        elif self.__status_code__ == 404:
            # 404 is reported if the resource doesn't exist
            raise NoSuchResourceError('Resource does not exist') from self
        # BAD_REQUEST is sent when:
        #  * An action is invalid given the current state
        #  * When a quota is exceeded
        #  * When a badly formatted request is sent
        #  * Who knows what else...!
        elif self.__error_code__ == 'BAD_REQUEST':
            # To distinguish, we need to check the message
            message = str(self).lower()
            if 'validation error' in message:
                raise BadRequestError('Badly formatted request') from self
            elif 'vdc has run out of' in message:
                raise QuotaExceededError('Quota exceeded for organisation') from self
            elif 'requested operation will exceed' in message:
                raise QuotaExceededError('Requested operation will exceed organisation quota') from self
            elif 'computer name can only contain' in message:
                raise InvalidMachineNameError('Must start with a letter and contain alphanumeric, - and . characters only') from self
            else:
                raise InvalidActionError(
                    'Action is invalid for current state') from self
        # DUPLICATE_NAME is sent when a name is duplicated (surprise...!)
        elif self.__error_code__ == 'DUPLICATE_NAME':
            raise DuplicateNameError('Name is already in use') from self
        # BUSY_ENTITY is sent when a task cannot be completed because another task is in effect...
        elif self.__error_code__ == 'BUSY_ENTITY':
            raise OperationInProgressError(
                'Another operation is using a resource required by this operation') from self
        # TASK_CANCELED is sent when a task is cancelled...
        elif self.__error_code__ == 'TASK_CANCELED':
            raise TaskCancelledError('Action cancelled') from self
        # And TASK_ABORTED is sent when an admin aborts a task...
        elif self.__error_code__ == 'TASK_ABORTED':
            raise TaskAbortedError('Action aborted by administrator') from self
        # Otherwise, assume the request was incorrectly specified by the implementation
        raise ImplementationError('Bad request') from self

    @classmethod
    def from_xml(cls, endpoint, user, error):
        """
        Creates and returns a new :py:class:`VCloudError` from the given XML. The
        XML can be given either as a string or as an ``ElementTree.Element``.

        Raises ``ValueError`` if the given XML string is not a valid vCD error.

        :param endpoint: The endpoint that produced the XML
        :param user: The user whose session produced the XML
        :param error: The XML or ElementTree Element containing a vCD error
        :returns: A :py:class:`VCloudError`
        """
        try:
            if not isinstance(error, ET.Element):
                error = ET.fromstring(error)
            return cls(
                endpoint, user,
                int(error.attrib['majorErrorCode']),
                error.attrib['minorErrorCode'].upper(),
                error.attrib['message']
            )
        except (ET.ParseError, ValueError, KeyError, AttributeError):
            raise ValueError('Given XML is not a valid vCD Error')


###############################################################################
###############################################################################


class VCloudSession(Session):
    """
    Session implementation for the vCloud Director 5.5 API.

    :param endpoint: The API endpoint
    :param auth_token: An API authorisation token for the session
    """
    def __init__(self, endpoint, user, password):
        self.__endpoint = endpoint.rstrip('/')
        self.__user = user

        # Create a requests session that can inject the required headers
        self.__session = requests.Session()
        self.__session.headers.update(_REQUIRED_HEADERS)

        # Get an auth token for the session and inject it into the headers for
        # future requests
        try:
            res = self.api_request('POST', 'sessions', auth = (user, password))
        except CloudServiceError:
            # Log some extra context with the error
            _log.error('Error while authenticating - {}, {}'.format(endpoint, user))
            raise
        auth_token = res.headers['x-vcloud-authorization']
        self.__session.headers.update({ 'x-vcloud-authorization' : auth_token })

    def __getstate__(self):
        """
        Called when the object is pickled
        """
        # All we need to reconstruct the session is the endpoint, user and auth token
        state = { 'endpoint' : self.__endpoint, 'user' : self.__user }
        if self.__session:
            state['auth_token'] = self.__session.headers['x-vcloud-authorization']
        return state

    def __setstate__(self, state):
        """
        Called when the object is unpickled
        """
        self.__endpoint = state['endpoint']
        self.__user = state['user']
        # Reconstruct the session object
        if 'auth_token' in state:
            self.__session = requests.Session()
            self.__session.headers.update(_REQUIRED_HEADERS)
            self.__session.headers.update({ 'x-vcloud-authorization' : state['auth_token'] })
        else:
            self.__session = None

    def api_request(self, method, path, *args, **kwargs):
        """
        Makes a request to the vCloud Director API, injecting auth headers etc.,
        and returns the response if it has a 20x status code.

        If the status code is not 20x, a relevant exception is thrown.

        :param method: HTTP method to use (case-insensitive)
        :param path: Path to request
                     Can be relative (endpoint is prepended) or fully-qualified
        :param \*args: Other positional arguments to be passed to ``requests``
        :param \*\*kwargs: Other keyword arguments to be passed to ``requests``
        :returns: The ``requests.Response``
        """
        # Deduce the path to use
        if not re.match(r'https?://', path):
            path = '/'.join([self.__endpoint, path.strip('/')])
        # Make the request
        if self.__session is None:
            raise InvalidActionError('Session has already been closed')
        func = getattr(self.__session, method.lower(), None)
        if func is None:
            raise ImplementationError('Invalid HTTP method - {}'.format(method))
        # Convert exceptions from requests into cloud service connection errors
        # Since we don't configure requests to throw HTTP exceptions (we deal
        # with status codes instead), if we see an exception it is a problem
        _log.info('[%s] [%s] %s request to %s',
                  self.__endpoint, self.__user, method.upper(), path)
        try:
            res = func(path, *args, verify = False, **kwargs)
        except requests.exceptions.RequestException:
            raise ProviderConnectionError('Cannot connect to vCloud Director API')
        # If the response status is an error (i.e. 4xx or 5xx), try to raise an
        # appropriate error, otherwise return the response
        if res.status_code == 503:
            # A 503 error probably means we couldn't even contact vCD
            raise ProviderConnectionError('Cannot connect to vCloud Director API')
        elif res.status_code >= 500:
            # Any other 5xx error indicates a problem on the server
            try:
                # Try to derive an exception from the vCD error
                VCloudError.from_xml(self.__endpoint, self.__user, res.text) \
                           .raise_cloud_service_error()
            except ValueError:
                # If creating a vCD error fails (probaby because there is not one
                # in the response), raise a more generic error
                raise ProviderUnavailableError('vCloud Director API encountered an error')
        elif res.status_code >= 400:
            try:
                # Try to create a cloud service error from the underlying vCD error
                VCloudError.from_xml(self.__endpoint, self.__user, res.text) \
                           .raise_cloud_service_error()
            except ValueError:
                # If there is no cloud error, raise the specific errors ourself
                # vCD only raises 401, 403 and 404 error codes
                if res.status_code == 401:
                    raise AuthenticationError('Authentication failed')
                elif res.status_code == 403:
                    raise PermissionsError('Insufficient permissions')
                elif res.status_code == 404:
                    raise NoSuchResourceError('Resource does not exist')
                else:
                    raise CloudServiceError('Unknown error with status code {}'.format(res.status_code))
        else:
            return res

    def wait_for_task(self, task_href):
        """
        Takes the href of a task and waits for it to complete before returning.

        If the task fails to complete successfully, it throws an exception with
        a suitable message.

        :param task_href: The href of the task to wait for
        """
        # Loop until we have success or failure
        while True:
            # Get the current status
            task = ET.fromstring(self.api_request('GET', task_href).text)
            status = task.attrib['status'].lower()
            # If the task is successful, we can exit
            if status == 'success':
                break
            # Try to find an Error in the Task, and raise the corresponding
            # cloud service error with the VCloudError as it's cause
            cs_err = None
            xml_err = task.find('.//vcd:Error', _NS)
            if xml_err is not None:
                try:
                    VCloudError.from_xml(self.__endpoint, self.__user, xml_err) \
                               .raise_cloud_service_error()
                except TaskFailedError:
                    # If the raised exception is already a task error, re-raise it
                    raise
                except CloudServiceError as e:
                    # All other cloud service errors should be wrapped in TaskFailedError
                    # to make the context clear
                    raise TaskFailedError(str(e)) from e
                except ValueError:
                    # A value error means there was not a valid error in the XML
                    # This is OK
                    pass
            # We should probably never get this far (if status is canceled, aborted
            # or error, there should be an Error element in the response)
            # But if we do, bail based on status
            if status == 'canceled':
                raise TaskCancelledError('Action cancelled')
            elif status == 'aborted':
                raise TaskAbortedError('Action aborted by administrator')
            elif status == 'error':
                raise TaskFailedError('Unrecoverable error')
            # Any other statuses, we sleep before fetching the task again
            sleep(_POLL_INTERVAL)

    _TYPE_KEY = '{{{}}}type'.format(_NS['xsi'])
    def get_metadata(self, base):
        """
        Finds all the metadata associated with the given base and returns it as
        a dictionary.

        :param base: The object to find metadata for
        :returns: A dictionary of metadata entries
        """
        try:
            xml = ET.fromstring(self.api_request('GET', '{}/metadata'.format(base)).text)
        except NoSuchResourceError:
            return {}
        meta = {}
        for entry in xml.findall('.//vcd:MetadataEntry', _NS):
            key = entry.find('./vcd:Key', _NS).text
            value = entry.find('.//vcd:Value', _NS).text
            type_ = entry.find('./vcd:TypedValue', _NS).attrib[self._TYPE_KEY]
            # Try to convert the value
            try:
                if type_ == 'MetadataNumberValue':
                    # Number actually means int, but the number can be in the format 10.0
                    try:
                        meta[key] = int(value)
                    except ValueError:
                        meta[key] = int(float(value))
                elif type_ == "MetadataBooleanValue":
                    meta[key] = (value.lower() == 'true')
                elif type_ == "MetadataDateTimeValue":
                    # Don't attempt to parse the timezone
                    meta[key] = datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
                else:
                    meta[key] = value
            except (ValueError, TypeError):
                raise BadConfigurationError('Invalid metadata value')
        return meta

    def poll(self):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.poll`.
        """
        # Just hit an API endpoint that does nothing but report session info
        self.api_request('GET', 'session')
        return True

    def has_permission(self, permission):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.has_permission`.
        """
        # This implementation uses vCD metadata attached to the org
        # So first, we get the href of the org for the session
        session = ET.fromstring(self.api_request('GET', 'session').text)
        org = session.find('.//vcd:Link[@type="application/vnd.vmware.vcloud.org+xml"]', _NS)
        # Then get the metadata
        meta = self.get_metadata(org.attrib['href'])
        # Add the namespace to the permission as the key into metadata
        #   If the key is not present, treat that as having value 0
        return bool(meta.get('JASMIN.{}'.format(permission.upper()), 0))

    def list_images(self):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.list_images`.

        .. note::

            This implementation uses `vAppTemplate` uuids as the image ids
        """
        # Get a list of uris of catalogs available to the user
        results = ET.fromstring(self.api_request('GET', 'catalogs/query').text)
        cat_refs = [result.attrib['href'] for result in results.findall('vcd:CatalogRecord', _NS)]
        # Now we know the catalogs we have access to, we can get the items
        images = []
        for cat_ref in cat_refs:
            # Query the catalog to find its items
            catalog = ET.fromstring(self.api_request('GET', cat_ref).text)
            # Query each item to find out if it is a vAppTemplate or some other
            # type of media (e.g. an ISO, which we want to ignore)
            for item_ref in catalog.findall('.//vcd:CatalogItem', _NS):
                item = ET.fromstring(self.api_request('GET', item_ref.attrib['href']).text)
                entity = item.find(
                    './/vcd:Entity[@type="application/vnd.vmware.vcloud.vAppTemplate+xml"]', _NS
                )
                # If there is no vAppTemplate, ignore the catalogue item
                if entity is None:
                    continue
                # get_image might still decide that the vAppTemplate is not one
                # we recognise, maybe because it is incorrectly configured
                try:
                    images.append(self.get_image(
                        entity.attrib['href'].rstrip('/').split('/').pop()
                    ))
                except NoSuchResourceError:
                    continue
        return images

    def get_image(self, image_id):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.get_image`.

        .. note::

            This implementation uses `vAppTemplate` uuids as the image ids
        """
        template = ET.fromstring(
            self.api_request('GET', 'vAppTemplate/{}'.format(image_id)).text
        )
        # If the template is not a gold master, reject it
        if template.attrib['goldMaster'].lower() != 'true':
            raise NoSuchResourceError('Image is not a gold master')
        # Strip the version string from the name
        name = '-'.join(template.attrib['name'].split('-')[:-1])
        description = template.findtext('vcd:Description', '', _NS)
        # If the template has a link to delete it, then it is private
        is_public = template.find('./vcd:Link[@rel="remove"]', _NS) is None
        # Fetch the metadata associated with the vAppTemplate
        meta = self.get_metadata(template.attrib['href'])
        # If a template has no NAT policy set, reject it
        try:
            nat_policy = NATPolicy[meta['JASMIN.NAT_POLICY'].upper()]
        except KeyError:
            raise NoSuchResourceError('Image has no NAT policy')
        # Use a default for host type if not available
        host_type = meta.get('JASMIN.HOST_TYPE', 'other')
        return Image(image_id, name, host_type, description, nat_policy, is_public)

    def image_from_machine(self, machine_id, name, description):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.image_from_machine`.

        .. note::

            This implementation uses `vAppTemplate` uuids as the image ids
        """
        # First, check if the session is allowed to do this!
        if not self.has_permission('CAN_CREATE_TEMPLATES'):
            raise PermissionsError('Insufficient permissions')
        # Find the catalogue we will create the image in
        # This is done by selecting the first catalogue from the org we are using
        # First, we have to retrieve the org from the session
        session = ET.fromstring(self.api_request('GET', 'session').text)
        org_ref = session.find('.//vcd:Link[@type="application/vnd.vmware.vcloud.org+xml"]', _NS)
        if org_ref is None:
            raise BadConfigurationError('Unable to find organisation for user')
        org = ET.fromstring(self.api_request('GET', org_ref.attrib['href']).text)
        # Then get the catalogue from the org
        cat_ref = org.find('.//vcd:Link[@type="application/vnd.vmware.vcloud.catalog+xml"]', _NS)
        if cat_ref is None:
            raise BadConfigurationError('Organisation has no catalogues with write access')
        # Before we create the catalogue item, we must power down the machine
        try:
            self.stop_machine(machine_id)
        except InvalidActionError:
            # If it is already powered down, great!
            pass
        # Send the request to create the catalogue item and wait for it to complete
        source_href = '{}/vApp/{}'.format(self.__endpoint, machine_id)
        payload = _ENV.get_template('CaptureVAppParams.xml').render({
            'image': {
                # Append todays date to the template name as a version string
                'name'        : '{}-{:%Y%m%d}'.format(name, datetime.now()),
                'description' : description,
                'source_href' : source_href,
            },
        })
        try:
            task = ET.fromstring(self.api_request(
                'POST', '{}/action/captureVApp'.format(cat_ref.attrib['href']), payload
            ).text)
        except ProviderUnavailableError:
            # For some reason, vCD throws a 500 error when a template with the given
            # name already exists
            # So we have no choice but to assume it is a duplicate name error
            raise DuplicateNameError('Name is already in use')
        try:
            self.wait_for_task(task.attrib['href'])
        except TaskFailedError as e:
            raise ImageCreateError('{} while creating catalogue item'.format(e)) from e
        # Get the id of the create vAppTemplate from the task
        template_ref = task.find(
            './/*[@type="application/vnd.vmware.vcloud.vAppTemplate+xml"]', _NS
        )
        template_id = template_ref.attrib['href'].rstrip('/').split('/').pop()
        # Write the associated metadata
        host_type = 'other'
        nat_policy = NATPolicy.USER
        payload = _ENV.get_template('VAppTemplateMetadata.xml').render({
            'host_type'  : host_type,
            'nat_policy' : nat_policy.name,
        })
        task = ET.fromstring(self.api_request(
            'POST', '{}/metadata'.format(template_ref.attrib['href']), payload
        ).text)
        try:
            self.wait_for_task(task.attrib['href'])
        except TaskFailedError as e:
            raise ImageCreateError('{} while creating catalogue item'.format(e)) from e
        # Delete the source machine
        self.delete_machine(machine_id)
        # Newly created templates are never public
        return Image(template_id, name, host_type, description, nat_policy, False)

    def delete_image(self, image_id):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.delete_image`.

        .. note::

            This implementation uses `vAppTemplate` uuids as the image ids
        """
        try:
            task = ET.fromstring(self.api_request(
                'DELETE', 'vAppTemplate/{}'.format(image_id)
            ).text)
            self.wait_for_task(task.attrib['href'])
        except (InvalidActionError, TaskFailedError) as e:
            raise ImageDeleteError('{} while deleting catalogue item'.format(e)) from e

    def count_machines(self):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.count_machines`.
        """
        # We only need one API query to return this
        results = ET.fromstring(self.api_request('GET', 'vApps/query').text)
        return len(results.findall('vcd:VAppRecord', _NS))

    def list_machines(self):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.list_machines`.
        """
        # This will return all the VMs available to the user
        results = ET.fromstring(self.api_request('GET', 'vApps/query').text)
        apps = results.findall('vcd:VAppRecord', _NS)
        return [
            self.get_machine(app.attrib['href'].rstrip('/').split('/').pop()) for app in apps
        ]

    def __gateway_from_app(self, app):
        """
        Given an ET element representing a vApp, returns an ET element representing the
        edge device for the network to which the primary NIC of the first VM in the vApp
        is connected, or None
        """
        try:
            vdc_ref = app.find('./vcd:Link[@type="application/vnd.vmware.vcloud.vdc+xml"]', _NS)
            vdc = ET.fromstring(self.api_request('GET', vdc_ref.attrib['href']).text)
            gateways_ref = vdc.find('./vcd:Link[@rel="edgeGateways"]', _NS)
            gateways = ET.fromstring(self.api_request('GET', gateways_ref.attrib['href']).text)
            # Assume one gateway per vdc
            gateway_ref = gateways.find('./vcd:EdgeGatewayRecord', _NS)
            return ET.fromstring(self.api_request('GET', gateway_ref.attrib['href']).text)
        except AttributeError:
            return None

    def __primary_nic_from_app(self, app):
        """
        Given an ET element representing a vApp, returns an ET element representing the primary
        NIC of the first VM within the vApp, or None
        """
        try:
            vm = app.find('.//vcd:Vm', _NS)
            primary_net_idx = vm.find('.//vcd:PrimaryNetworkConnectionIndex', _NS).text
            return vm.find(
                './/vcd:NetworkConnection[vcd:NetworkConnectionIndex="{}"]'.format(primary_net_idx), _NS
            )
        except AttributeError:
            return None

    def __internal_ip_from_app(self, app):
        """
        Given an ET element representing a vApp, returns the internal IP of the primary
        NIC of the first VM within the vApp, or None
        """
        try:
            nic = self.__primary_nic_from_app(app)
            return IPv4Address(nic.find('vcd:IpAddress', _NS).text)
        except (AttributeError, AddressValueError):
            return None

    _CAPACITY_KEY = '{{{}}}capacity'.format(_NS['vcd'])
    def get_machine(self, machine_id):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.get_machine`.
        """
        app = ET.fromstring(self.api_request('GET', 'vApp/{}'.format(machine_id)).text)
        name = app.attrib['name']
        # If the app has an outstanding task with status="error", set machine status
        # to 'unspecified error'
        if app.find('.//vcd:Task[@status="error"]', _NS) is not None:
            status = MachineStatus.ERROR
        else:
            # Otherwise, convert the integer status to one of the statuses in MachineStatus
            status = _STATUS_MAP.get(int(app.attrib['status']), MachineStatus.UNRECOGNISED)
        # Get the description
        description = app.findtext('vcd:Description', '', _NS)
        # Convert the string creationDate to a datetime
        # For now, make no attempt to process timezone
        created = datetime.strptime(
            app.find('vcd:DateCreated', _NS).text[:19], '%Y-%m-%dT%H:%M:%S'
        )
        # For OS, CPU, RAM and disks, we use the values from the first VM
        vm = app.find('.//vcd:Vm', _NS)
        if vm is not None:
            os = vm.findtext(
                './/ovf:OperatingSystemSection/ovf:Description', 'Unknown', _NS
            )
            vhs = vm.find('.//ovf:VirtualHardwareSection', _NS)
            cpus = int(vhs.findtext(
                './ovf:Item[rasd:ResourceType="3"]/rasd:VirtualQuantity', '-1', _NS
            ))
            ram = int(vhs.findtext(
                './ovf:Item[rasd:ResourceType="4"]/rasd:VirtualQuantity', '-1', _NS
            )) // 1024  # RAM comes out in MB - convert to GB
            # Find the disks associated with the VM
            disks = []
            for disk in vhs.findall('./ovf:Item[rasd:ResourceType="17"]', _NS):
                disk_name = disk.find('./rasd:ElementName', _NS).text
                size = disk.find('./rasd:HostResource', _NS).attrib[self._CAPACITY_KEY]
                size = int(size) // 1024  # Disk size comes out in MB
                disks.append(HardDisk(disk_name, size))
            disks = tuple(disks)
        else:
            os = 'Unknown'
            cpus = -1
            ram = -1
            disks = tuple()
        # For IP addresses, we use the values from the primary NIC of the first VM
        internal_ip = self.__internal_ip_from_app(app)
        external_ip = None
        # If there is no internal IP, don't even bother trying to find an external one...
        if internal_ip is not None:
            # Try to find a corresponding DNAT rule for an external IP
            try:
                gateway = self.__gateway_from_app(app)
                nat_rules = gateway.findall('.//vcd:NatRule', _NS)
            except AttributeError:
                nat_rules = []
            for rule in nat_rules:
                if rule.find('vcd:RuleType', _NS).text.upper() == 'DNAT':
                    # Check if this rule applies to our IP
                    translated = IPv4Address(rule.find('.//vcd:TranslatedIp', _NS).text)
                    if translated == internal_ip:
                        external_ip = IPv4Address(rule.find('.//vcd:OriginalIp', _NS).text)
                        break
        # Get the username of the owner
        user = app.find('.//vcd:Owner/vcd:User', _NS)
        owner = user.attrib['name'] if user is not None else None
        return Machine(machine_id, name, status, cpus, ram, disks,
                       description, created, os, internal_ip, external_ip, owner)

    # Guest customisation script expects a script to be baked into each template
    # at /usr/local/bin/activator.sh
    # The script should have the following interface on all machines:
    #   activator.sh <ssh_key> <org_name> <vm_type> <vm_id>
    # However, the script itself may differ from machine to machine, and is free
    # to use or ignore the arguments as it sees fit
    _GUEST_CUSTOMISATION = """#!/bin/sh
if [ x$1 = x"postcustomization" ]; then
  /usr/local/bin/activator.sh "{ssh_key}" "{org_name}" "{vm_type}" "{vm_id}"
fi
"""
    def provision_machine(self, image_id, name, description, ssh_key, expose):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.provision_machine`.

        .. note::

            This implementation uses `vAppTemplate` uuids as the image ids
        """
        # Get the image info
        image = self.get_image(image_id)
        # Override expose based on the NAT policy
        if image.nat_policy == NATPolicy.ALWAYS:
            expose = True
        elif image.nat_policy == NATPolicy.NEVER:
            expose = False
        # Get the actual vAppTemplate XML
        template = ET.fromstring(
            self.api_request('GET', 'vAppTemplate/{}'.format(image_id)).text
        )
        # Get the current org from the session
        session = ET.fromstring(self.api_request('GET', 'session').text)
        org_ref = session.find('.//vcd:Link[@type="application/vnd.vmware.vcloud.org+xml"]', _NS)
        if org_ref is None:
            raise BadConfigurationError('Unable to find organisation for user')
        org = ET.fromstring(self.api_request('GET', org_ref.attrib['href']).text)
        # Configure each VM contained in the vApp
        vm_configs = []
        # Track the maximum number of NICs for a VM
        # The VDC is required to provide a network for each NIC
        n_networks_required = 0
        for idx, vm in enumerate(template.findall('.//vcd:Vm', _NS)):
            # Get all the network connections associated with the VM
            nics = vm.findall('.//vcd:NetworkConnection', _NS)
            if not nics:
                raise BadConfigurationError('No network connection section for VM')
            n_nics = len(nics)
            n_networks_required = max(n_networks_required, n_nics)
            # Get a UUID for the VM
            vm_id = uuid.uuid4().hex
            # Get a unique name for the VM
            #   The first VM has the same name as the vApp
            #   Subsequent VMs have the index appended
            vm_name = '{}{}'.format(name, idx if idx > 0 else '')
            # Make sure the guest customisation script is escaped after formatting
            script = self._GUEST_CUSTOMISATION.format(
                ssh_key  = ssh_key.strip(),
                org_name = org.attrib['name'],
                vm_type  = image.host_type,
                vm_id    = vm_id,
            )
            vm_configs.append({
                'href'          : vm.attrib['href'],
                'name'          : vm_name,
                'n_nics'        : n_nics,
                'customisation' : script,
            })
        # Get the VDC to deploy the VM into
        # This is done by selecting the first VCD from the org we are using
        vdc_ref = org.find('.//vcd:Link[@type="application/vnd.vmware.vcloud.vdc+xml"]', _NS)
        if vdc_ref is None:
            raise BadConfigurationError('Organisation has no VDCs')
        vdc = ET.fromstring(self.api_request('GET', vdc_ref.attrib['href']).text)
        # Find the available networks from the VDC
        network_refs = vdc.findall('.//vcd:AvailableNetworks/vcd:Network', _NS)
        if not network_refs:
            raise BadConfigurationError('No networks available in vdc')
        # Get the mapping of NIC => network from the network metadata
        networks = {}
        for network_ref in network_refs:
            # Get the NIC_ID metadata
            # Ignore any networks without it
            metadata = self.get_metadata(network_ref.attrib['href'])
            try:
                nic_id = metadata['JASMIN.NIC_ID']
            except KeyError:
                continue
            # Store the network config against the NIC
            networks[nic_id] = { 'name' : network_ref.attrib['name'],
                                 'href' : network_ref.attrib['href'] }
        # Check that there is a network for each NIC
        for n in range(n_networks_required):
            if n not in networks:
                raise BadConfigurationError('No network for NIC {}'.format(n))
        # Build the XML payload for the request
        payload = _ENV.get_template('ComposeVAppParams.xml').render({
            'appliance': {
                'name'        : name,
                'description' : description,
                'vms'         : vm_configs,
            },
            'networks': networks,
        })
        # Send the request to vCD
        # The response is a vapp object
        app = ET.fromstring(self.api_request(
            'POST', '{}/action/composeVApp'.format(vdc.attrib['href']), payload
        ).text)
        # The vapp has a list of tasks associated with it
        # We keep checking that list until it has nothing in it
        while True:
            tasks = app.findall('./vcd:Tasks/vcd:Task', _NS)
            # If there are no tasks, we're done
            if not tasks: break
            # Wait for each task to complete
            for task in tasks:
                try:
                    self.wait_for_task(task.attrib['href'])
                except TaskFailedError as e:
                    raise ProvisioningError('{} while provisioning machine'.format(e)) from e
            # Refresh our view of the app
            app = ET.fromstring(self.api_request('GET', app.attrib['href']).text)
        machine_id = app.attrib['href'].rstrip('/').split('/').pop()
        # Expose the machine if required
        if expose:
            self.__expose_machine(machine_id)
        return self.get_machine(machine_id)

    def __expose_machine(self, machine_id):
        """
        Applies NAT and firewall rules to expose the given machine to the internet.
        """
        # We need to access the edge device that the machine is connected to the internet via
        # To do this, we first get the machine details, then the vdc details
        app = ET.fromstring(self.api_request('GET', 'vApp/{}'.format(machine_id)).text)
        gateway = self.__gateway_from_app(app)
        if gateway is None:
            raise BadConfigurationError('Could not find edge gateway')
        # Find the uplink gateway interface (assume there is only one)
        uplink = gateway.find('.//vcd:GatewayInterface[vcd:InterfaceType="uplink"]', _NS)
        if uplink is None:
            raise BadConfigurationError('Edge gateway has no uplink')
        # Find the pool of available external IP addresses
        ip_pool = set()
        for ip_range in uplink.findall('.//vcd:IpRange', _NS):
            start_ip = IPv4Address(ip_range.find('./vcd:StartAddress', _NS).text)
            end_ip = IPv4Address(ip_range.find('./vcd:EndAddress', _NS).text)
            ip_pool |= set(ip for net in summarize_address_range(start_ip, end_ip) for ip in net)
        # Find our internal IP address
        internal_ip = self.__internal_ip_from_app(app)
        if internal_ip is None:
            raise NetworkingError('Machine has no network connections')
        # Search the existing NAT rules:
        #   1. If we find an existing DNAT rule specifically for our IP, we are done
        #      We assume that all NAT configuration was done by this method, in which case the
        #      DNAT, SNAT and firewall rules should all be set in one call, and so if one
        #      exists, the others also will
        #   2. Remove ip addresses that already have an associated NAT rule from the pool
        nat_rules = gateway.findall('.//vcd:NatRule', _NS)
        for rule in nat_rules:
            rule_type = rule.find('vcd:RuleType', _NS).text.upper()
            if rule_type == 'SNAT':
                # For SNAT rules, we rule out the translated IP
                ip_pool.discard(IPv4Address(rule.find('.//vcd:TranslatedIp', _NS).text))
            elif rule_type == 'DNAT':
                # Check if this rule applies to our IP
                translated = IPv4Address(rule.find('.//vcd:TranslatedIp', _NS).text)
                if translated == internal_ip:
                    # Machine is already exposed, so nothing to do
                    return
                # For DNAT rules, we rule out the original IP
                ip_pool.discard(IPv4Address(rule.find('.//vcd:OriginalIp', _NS).text))
        try:
            ip_use = ip_pool.pop()
        except KeyError:
            raise NetworkingError('No external IP addresses available')
        # Get the current edge gateway configuration
        gateway_config = gateway.find('.//vcd:EdgeGatewayServiceConfiguration', _NS)
        if gateway_config is None:
            raise BadConfigurationError('No edge gateway configuration exists')
        # Get the NAT service section
        # If there is no NAT service section, create one
        nat_service = gateway_config.find('vcd:NatService', _NS)
        if nat_service is None:
            nat_service = ET.fromstring(_ENV.get_template('NatService.xml').render())
            gateway_config.append(nat_service)
        network = uplink.find('vcd:Network', _NS)
        details = {
            'description' : 'Public facing IP',
            'network' : {
                'href' : network.attrib['href'],
                'name' : network.attrib['name'],
            },
            'external_ip' : ip_use,
            'internal_ip' : internal_ip,
        }
        # Prepend a new SNAT rule to the service
        # The first element is always IsEnabled, so we insert at index 1
        # NOTE: It is important that this is PREPENDED - to ensure that machine appears to
        #       the outside world with a specific IP it must appear before any generic
        #       SNAT rules
        nat_service.insert(1, ET.fromstring(_ENV.get_template('SNATRule.xml').render(details)))
        # Append a new DNAT rule to the service
        nat_service.append(ET.fromstring(_ENV.get_template('DNATRule.xml').render(details)))
        # Get the firewall service and append a new rule
        firewall = gateway_config.find('vcd:FirewallService', _NS)
        if firewall is None:
            raise BadConfigurationError('No firewall configuration defined')
        firewall.append(ET.fromstring(_ENV.get_template('InboundFirewallRule.xml').render(details)))
        # Make the changes
        edit_url = '{}/action/configureServices'.format(gateway.attrib['href'])
        task = ET.fromstring(self.api_request(
            'POST', edit_url, ET.tostring(gateway_config),
            headers = { 'Content-Type' : 'application/vnd.vmware.admin.edgeGatewayServiceConfiguration+xml' }
        ).text)
        try:
            self.wait_for_task(task.attrib['href'])
        except TaskFailedError as e:
            raise NetworkingError('{} while applying network configuration'.format(e)) from e

    def __unexpose_machine(self, machine_id):
        """
        Removes any NAT and firewall rules applied to the given machine.
        """
        # We need to access the edge device that the machine is connected to the internet via
        # To do this, we first get the machine details, then the vdc details
        app = ET.fromstring(self.api_request('GET', 'vApp/{}'.format(machine_id)).text)
        gateway = self.__gateway_from_app(app)
        if gateway is None:
            raise BadConfigurationError('Could not find edge gateway')
        # Find our internal IP address
        internal_ip = self.__internal_ip_from_app(app)
        if internal_ip is None:
            return
        # Get the current edge gateway configuration
        # If none exists, there aren't any NAT rules
        gateway_config = gateway.find('.//vcd:EdgeGatewayServiceConfiguration', _NS)
        if gateway_config is None:
            return
        # Get the NAT service section
        # If there is no NAT service section, there aren't any NAT rules
        nat_service = gateway_config.find('vcd:NatService', _NS)
        if nat_service is None:
            return
        # Remove any NAT rules from the service that apply specifically to our internal IP
        # As we go, we save the mapped external ip in order to remove firewall rules after
        nat_rules = nat_service.findall('vcd:NatRule', _NS)
        external_ip = None
        for rule in nat_rules:
            rule_type = rule.find('vcd:RuleType', _NS).text.upper()
            if rule_type == 'SNAT':
                # For SNAT rules, we check the original ip
                # The default SNAT rule has a /24 network, so we need to catch the AddressValueError
                try:
                    original = IPv4Address(rule.find('.//vcd:OriginalIp', _NS).text)
                    if original == internal_ip:
                        nat_service.remove(rule)
                        # The external IP is the translated ip, and should NEVER be a network
                        external_ip = IPv4Address(rule.find('.//vcd:TranslatedIp', _NS).text)
                except AddressValueError:
                    # We should only get to here if original ip is a network, which we ignore
                    pass
            elif rule_type == 'DNAT':
                # For DNAT rules, we check the translated ip, which should NEVER be a network
                translated = IPv4Address(rule.find('.//vcd:TranslatedIp', _NS).text)
                if translated == internal_ip:
                    nat_service.remove(rule)
                    # The external ip is the original ip
                    external_ip = IPv4Address(rule.find('.//vcd:OriginalIp', _NS).text)
        # If we didn't find an external ip, the machine must not be exposed
        if external_ip is None:
            return
        # Remove any firewall rules (inbound or outbound) that apply specifically to the ip
        firewall = gateway_config.find('vcd:FirewallService', _NS)
        if firewall is None:
            # If we get to here, we would expect a firewall config to exist
            raise BadConfigurationError('No firewall configuration defined')
        firewall_rules = firewall.findall('vcd:FirewallRule', _NS)
        for rule in firewall_rules:
            # Try the source and destination ips independently
            # We ignore AddressValueErrors, since the ip couldn't possibly match
            try:
                source_ip = IPv4Address(rule.find('vcd:SourceIp', _NS).text)
                if source_ip == external_ip:
                    firewall.remove(rule)
            except AddressValueError:
                pass
            try:
                dest_ip = IPv4Address(rule.find('vcd:DestinationIp', _NS).text)
                if dest_ip == external_ip:
                    firewall.remove(rule)
            except AddressValueError:
                pass
        # Make the changes
        edit_url = '{}/action/configureServices'.format(gateway.attrib['href'])
        task = ET.fromstring(self.api_request(
            'POST', edit_url, ET.tostring(gateway_config),
            headers = { 'Content-Type' : 'application/vnd.vmware.admin.edgeGatewayServiceConfiguration+xml' }
        ).text)
        try:
            self.wait_for_task(task.attrib['href'])
        except TaskFailedError as e:
            raise NetworkingError('{} while applying network configuration'.format(e)) from e

    def reconfigure_machine(self, machine_id, cpus, ram):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.reconfigure_machine`.
        """
        # First, get the machine
        machine = self.get_machine(machine_id)
        # The machine must be off
        if machine.status != MachineStatus.POWERED_OFF:
            raise InvalidActionError('Machine must be powered off to reconfigure')
        # If there are no changes, just return
        if cpus == machine.cpus and ram == machine.ram:
            return machine
        # Get the id of the first VM in the vApp
        app = ET.fromstring(self.api_request('GET', 'vApp/{}'.format(machine_id)).text)
        vm_id = app.find('.//vcd:Vm', _NS).attrib['href'].rstrip('/').split('/').pop()
        # Change the CPU if it is different to the previous
        if cpus != machine.cpus:
            payload = _ENV.get_template('CPUHardwareSection.xml').render({
                'cpus' : cpus,
            })
            cpu_task = ET.fromstring(self.api_request(
                'PUT', 'vApp/{}/virtualHardwareSection/cpu'.format(vm_id), payload
            ).text)
            try:
                self.wait_for_task(cpu_task.attrib['href'])
            except TaskFailedError as e:
                raise ResourceAllocationError(
                    '{} while allocating {} cores'.format(e, cpus)
                ) from e
        # Change the memory if it is different to the previous
        if ram != machine.ram:
            payload = _ENV.get_template('MemoryHardwareSection.xml').render({
                # Convert GB to MB
                'ram' : ram * 1024,
            })
            mem_task = ET.fromstring(self.api_request(
                'PUT', 'vApp/{}/virtualHardwareSection/memory'.format(vm_id), payload
            ).text)
            try:
                self.wait_for_task(mem_task.attrib['href'])
            except TaskFailedError as e:
                raise ResourceAllocationError(
                    '{} while allocating {} GB RAM'.format(e, ram)
                ) from e
        return self.get_machine(machine_id)

    _BUS_TYPE_KEY = '{{{}}}busType'.format(_NS['vcd'])
    _BUS_SUBTYPE_KEY = '{{{}}}busSubType'.format(_NS['vcd'])
    def add_disk_to_machine(self, machine_id, size):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.add_disk_to_machine`.
        """
        # First, check if the session is allowed to do this!
        if not self.has_permission('CAN_ADD_DISK'):
            raise PermissionsError('Insufficient permissions')
        # Check that the machine if off
        if self.get_machine(machine_id).status != MachineStatus.POWERED_OFF:
            raise InvalidActionError('Machine must be powered off to add a disk')
        # We will work with the first VM in the vApp
        app = ET.fromstring(self.api_request('GET', 'vApp/{}'.format(machine_id)).text)
        vm_id = app.find('.//vcd:Vm', _NS).attrib['href'].rstrip('/').split('/').pop()
        # Get the current disk info from the virtual hardware section
        disks_section = ET.fromstring(self.api_request(
            'GET', 'vApp/{}/virtualHardwareSection/disks'.format(vm_id)
        ).text)
        disks = disks_section.findall('./vcd:Item[rasd:ResourceType="17"]', _NS)
        # There must be at least one disk to use as a template
        if not disks:
            raise BadConfigurationError('Machine must have at least one disk before operation')
        # Find the maximum unit number and instance ID for all current disks
        max_unit = -1
        max_id = -1
        for disk in disks:
            max_unit = max(max_unit, int(disk.find('./rasd:AddressOnParent', _NS).text))
            max_id = max(max_id, int(disk.find('./rasd:InstanceID', _NS).text))
        # Create a new disk element
        new_disk = ET.fromstring(_ENV.get_template('HardDisk.xml').render({
            # Increase the discovered unit and ID by 1 for the new disk
            'unit' : max_unit + 1,
            'instance_id' : max_id + 1,
            # Capacity should be given in MB
            'capacity' : size * 1024,
            # Take bus and parent info from the first disk
            'bus_sub_type' : disks[0].find('./rasd:HostResource', _NS).attrib[self._BUS_SUBTYPE_KEY],
            'bus_type' : disks[0].find('./rasd:HostResource', _NS).attrib[self._BUS_TYPE_KEY],
            'parent' : disks[0].find('./rasd:Parent', _NS).text,
        }))
        # Add the disk to the disks section
        disks_section.append(new_disk)
        # Initiate the changes
        edit_url = 'vApp/{}/virtualHardwareSection/disks'.format(vm_id)
        task = ET.fromstring(self.api_request(
            'PUT', edit_url, ET.tostring(disks_section),
            headers = { 'Content-Type' : 'application/vnd.vmware.vcloud.rasdItemsList+xml' }
        ).text)
        # Wait for the task to complete
        try:
            self.wait_for_task(task.attrib['href'])
        except TaskFailedError as e:
            raise ResourceAllocationError(
                '{} while allocating {} GB hard disk'.format(e, size)
            ) from e
        return self.get_machine(machine_id)

    def start_machine(self, machine_id):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.start_machine`.
        """
        try:
            task = ET.fromstring(self.api_request(
                'POST', 'vApp/{}/power/action/powerOn'.format(machine_id)
            ).text)
            self.wait_for_task(task.attrib['href'])
        except InvalidActionError:
            # Swallow invalid action errors, as they don't really matter
            pass
        except TaskFailedError as e:
            raise PowerActionError('{} while starting machine'.format(e)) from e

    def stop_machine(self, machine_id):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.stop_machine`.
        """
        payload = _ENV.get_template('UndeployVAppParams.xml').render()
        try:
            task = ET.fromstring(self.api_request(
                'POST', 'vApp/{}/action/undeploy'.format(machine_id), payload
            ).text)
            self.wait_for_task(task.attrib['href'])
        except InvalidActionError:
            # Swallow invalid action errors, as they don't really matter
            pass
        except TaskFailedError as e:
            raise PowerActionError('{} while stopping machine'.format(e)) from e

    def restart_machine(self, machine_id):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.restart_machine`.
        """
        try:
            task = ET.fromstring(self.api_request(
                'POST', 'vApp/{}/power/action/reset'.format(machine_id)
            ).text)
            self.wait_for_task(task.attrib['href'])
        except InvalidActionError:
            # Swallow invalid action errors, as they don't really matter
            pass
        except TaskFailedError as e:
            raise PowerActionError('{} while restarting machine'.format(e)) from e

    def delete_machine(self, machine_id):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.delete_machine`.
        """
        # Before deleting a machine, we make sure it is off
        self.stop_machine(machine_id)
        # We also want to remove any exposure to the internet
        # If we don't, we risk exposing the next machine that picks up the local
        # IP address from the pool
        self.__unexpose_machine(machine_id)
        try:
            task = ET.fromstring(self.api_request(
                'DELETE', 'vApp/{}'.format(machine_id)
            ).text)
            self.wait_for_task(task.attrib['href'])
        except TaskFailedError as e:
            raise PowerActionError('{} while deleting machine'.format(e)) from e

    def close(self):
        """
        See :py:meth:`jasmin_cloud.cloudservices.Session.close`.
        """
        if self.__session is None:
            # Already closed, so nothing to do
            return
        # Send a request to vCD to kill our session
        # We catch any errors and swallow them, since this could be called when an
        # exception has been thrown by a context manager
        try:
            self.api_request('DELETE', 'session')
            self.__session.close()
        except Exception:
            pass
        finally:
            self.__session = None
