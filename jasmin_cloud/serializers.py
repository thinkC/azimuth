"""
Django REST framework serializers for the ``jasmin_cloud`` app.

These serializers marshall objects from the :py:mod:`.provider.dto` package.
"""

import collections

from django.urls import reverse

from rest_framework import serializers


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(style = { 'input_type' : 'password' })


class TenancySerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only = True)
    name = serializers.CharField(read_only = True)

    def to_representation(self, obj):
        result = super().to_representation(obj)
        # If the info to build a link is in the context, add it
        request = self.context.get('request')
        if request:
            result.setdefault('links', {}).update({
                'quotas' : request.build_absolute_uri(
                    reverse('jasmin_cloud:quotas', kwargs = {
                        'tenant' : obj.id,
                    })
                ),
                'images' : request.build_absolute_uri(
                    reverse('jasmin_cloud:images', kwargs = {
                        'tenant' : obj.id,
                    })
                ),
                'sizes' : request.build_absolute_uri(
                    reverse('jasmin_cloud:sizes', kwargs = {
                        'tenant' : obj.id,
                    })
                ),
                'volumes' : request.build_absolute_uri(
                    reverse('jasmin_cloud:volumes', kwargs = {
                        'tenant' : obj.id,
                    })
                ),
                'volume_attachments' : request.build_absolute_uri(
                    reverse('jasmin_cloud:volume_attachments', kwargs = {
                        'tenant' : obj.id,
                    })
                ),
                'external_ips' : request.build_absolute_uri(
                    reverse('jasmin_cloud:external_ips', kwargs = {
                        'tenant' : obj.id,
                    })
                ),
                'machines' : request.build_absolute_uri(
                    reverse('jasmin_cloud:machines', kwargs = {
                        'tenant' : obj.id,
                    })
                ),
            })
        return result


class QuotaSerializer(serializers.Serializer):
    resource = serializers.CharField(read_only = True)
    units = serializers.CharField(read_only = True, allow_null = True)
    allocated = serializers.IntegerField(read_only = True)
    used = serializers.IntegerField(read_only = True)


class ImageRefSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only = True)

    def to_representation(self, obj):
        result = super().to_representation(obj)
        # If the info to build a link is in the context, add it
        request = self.context.get('request')
        tenant = self.context.get('tenant')
        if request and tenant:
            result.setdefault('links', {})['self'] = request.build_absolute_uri(
                reverse('jasmin_cloud:image_details', kwargs = {
                    'tenant' : tenant,
                    'image' : obj.id,
                })
            )
        return result


class SizeRefSerializer(serializers.Serializer):
    id = serializers.RegexField('^[a-z0-9-]+$', read_only = True)

    def to_representation(self, obj):
        result = super().to_representation(obj)
        # If the info to build a link is in the context, add it
        request = self.context.get('request')
        tenant = self.context.get('tenant')
        if request and tenant:
            result.setdefault('links', {})['self'] = request.build_absolute_uri(
                reverse('jasmin_cloud:size_details', kwargs = {
                    'tenant' : tenant,
                    'size' : obj.id,
                })
            )
        return result


class VolumeRefSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only = True)

    def to_representation(self, obj):
        result = super().to_representation(obj)
        # If the info to build a link is in the context, add it
        request = self.context.get('request')
        tenant = self.context.get('tenant')
        if request and tenant:
            result.setdefault('links', {})['self'] = request.build_absolute_uri(
                reverse('jasmin_cloud:volume_details', kwargs = {
                    'tenant' : tenant,
                    'volume' : obj.id,
                })
            )
        return result


class VolumeAttachmentRefSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only = True)

    def to_representation(self, obj):
        result = super().to_representation(obj)
        # If the info to build a link is in the context, add it
        request = self.context.get('request')
        tenant = self.context.get('tenant')
        if request and tenant:
            result.setdefault('links', {})['self'] = request.build_absolute_uri(
                reverse('jasmin_cloud:volume_attachment_details', kwargs = {
                    'tenant' : tenant,
                    'attachment' : obj.id,
                })
            )
        return result


class MachineRefSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only = True)

    def to_representation(self, obj):
        result = super().to_representation(obj)
        # If the info to build a link is in the context, add it
        request = self.context.get('request')
        tenant = self.context.get('tenant')
        if request and tenant:
            result.setdefault('links', {}).update({
                'self' : request.build_absolute_uri(
                    reverse('jasmin_cloud:machine_details', kwargs = {
                        'tenant' : tenant,
                        'machine' : obj.id,
                    })
                ),
            })
        return result


class ImageSerializer(ImageRefSerializer):
    name = serializers.CharField(read_only = True)
    is_public = serializers.BooleanField(read_only = True)
    nat_allowed = serializers.BooleanField(read_only = True)
    size = serializers.DecimalField(
        None, # No limit on the number of digits
        decimal_places = 2,
        coerce_to_string = False,
        read_only = True
    )


class SizeSerializer(SizeRefSerializer):
    name = serializers.CharField(read_only = True)
    cpus = serializers.IntegerField(read_only = True)
    ram = serializers.IntegerField(read_only = True)
    disk = serializers.IntegerField(read_only = True)


Ref = collections.namedtuple('Ref', ['id'])


class VolumeSerializer(VolumeRefSerializer):
    name = serializers.CharField()
    status = serializers.CharField(source = 'status.name', read_only = True)
    size = serializers.IntegerField(min_value = 1)
    attachments = VolumeAttachmentRefSerializer(many = True, read_only = True)

    def to_representation(self, obj):
        # Convert attachment ids to attachment refs before serializing
        obj.attachments = [Ref(a) for a in obj.attachment_ids]
        return super().to_representation(obj)


class VolumeAttachmentSerializer(VolumeAttachmentRefSerializer):
    machine = MachineRefSerializer(read_only = True)
    machine_id = serializers.UUIDField(write_only = True)

    volume = VolumeRefSerializer(read_only = True)
    volume_id = serializers.UUIDField(write_only = True)

    device = serializers.CharField(read_only = True)

    def to_representation(self, obj):
        # Convert raw ids to attachment refs before serializing
        obj.machine = Ref(obj.machine_id)
        obj.volume = Ref(obj.volume_id)
        return super().to_representation(obj)


class MachineStatusSerializer(serializers.Serializer):
    name = serializers.CharField(read_only = True)
    type = serializers.CharField(source = 'type.name', read_only = True)
    details = serializers.CharField(read_only = True)

class MachineSerializer(MachineRefSerializer):
    name = serializers.RegexField('^[a-z0-9\.\-_]+$')

    image = ImageRefSerializer(read_only = True)
    image_id = serializers.UUIDField(write_only = True)

    size = SizeRefSerializer(read_only = True)
    size_id = serializers.RegexField('^[a-z0-9-]+$', write_only = True)

    status = MachineStatusSerializer(read_only = True)
    power_state = serializers.CharField(read_only = True)
    task = serializers.CharField(read_only = True)
    fault = serializers.CharField(read_only = True)
    internal_ip = serializers.IPAddressField(read_only = True)
    external_ip = serializers.IPAddressField(read_only = True)
    nat_allowed = serializers.BooleanField(read_only = True)
    attachments = VolumeAttachmentRefSerializer(many = True, read_only = True)
    owner = serializers.CharField(read_only = True)
    created = serializers.DateTimeField(read_only = True)

    def to_representation(self, obj):
        # Convert raw ids to attachment refs before serializing
        obj.image = Ref(obj.image_id)
        obj.size = Ref(obj.size_id)
        obj.attachments = [Ref(a) for a in obj.attachment_ids]
        result = super().to_representation(obj)
        # If the info to build a link is in the context, add it
        request = self.context.get('request')
        tenant = self.context.get('tenant')
        if request and tenant:
            result.setdefault('links', {}).update({
                'start' : request.build_absolute_uri(
                    reverse('jasmin_cloud:machine_start', kwargs = {
                        'tenant' : tenant,
                        'machine' : obj.id,
                    })
                ),
                'stop' : request.build_absolute_uri(
                    reverse('jasmin_cloud:machine_stop', kwargs = {
                        'tenant' : tenant,
                        'machine' : obj.id,
                    })
                ),
                'restart' : request.build_absolute_uri(
                    reverse('jasmin_cloud:machine_restart', kwargs = {
                        'tenant' : tenant,
                        'machine' : obj.id,
                    })
                ),
            })
        return result


class ExternalIPSerializer(serializers.Serializer):
    external_ip = serializers.IPAddressField(read_only = True)
    machine_id = serializers.UUIDField(allow_null = True)

    def to_representation(self, obj):
        result = super().to_representation(obj)
        # If the info to build a link is in the context, add it
        request = self.context.get('request')
        tenant = self.context.get('tenant')
        if request and tenant:
            result.setdefault('links', {})['self'] = request.build_absolute_uri(
                reverse('jasmin_cloud:external_ip_details', kwargs = {
                    'tenant' : tenant,
                    'ip' : obj.external_ip,
                })
            )
        return result
