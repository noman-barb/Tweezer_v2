"""Shared access to ImageExchange protobuf messages.

The module first tries to import generated classes from ``image_exchange_pb2``.
If those modules are missing, it falls back to defining the schema dynamically so
that development does not strictly depend on generated code being present.
"""

from __future__ import annotations

from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory, symbol_database
from google.protobuf import empty_pb2, struct_pb2

__all__ = [
    "ImageChunk",
    "UploadAck",
    "LatestImageRequest",
    "LatestImageReply",
    "FrameEnvelope",
    "StorageConfig",
    "TrackDetectionProto",
    "ensure_registered",
]

_sym_db = symbol_database.Default()
_pool = descriptor_pool.DescriptorPool()
_factory = message_factory.MessageFactory(_pool)


def ensure_registered() -> None:
    """Ensure the ImageExchange descriptors exist in the global pool."""

    try:
        _pool.FindFileByName("image_exchange.proto")
        return
    except KeyError:
        pass

    # Register well-known dependencies in the private pool when missing.
    for dependency in (empty_pb2.DESCRIPTOR, struct_pb2.DESCRIPTOR):
        try:
            _pool.FindFileByName(dependency.name)
        except KeyError:
            _pool.AddSerializedFile(dependency.serialized_pb)

    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "image_exchange.proto"
    file_proto.package = "images"
    file_proto.syntax = "proto3"
    file_proto.dependency.extend(["google/protobuf/empty.proto", "google/protobuf/struct.proto"])

    message = file_proto.message_type.add()
    message.name = "ImageChunk"
    field = message.field.add()
    field.name = "filename"
    field.number = 1
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "timestamp_ms"
    field.number = 2
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
    field = message.field.add()
    field.name = "data"
    field.number = 3
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_BYTES
    field = message.field.add()
    field.name = "source"
    field.number = 4
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "sequence"
    field.number = 5
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64

    message = file_proto.message_type.add()
    message.name = "UploadAck"
    field = message.field.add()
    field.name = "ok"
    field.number = 1
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_BOOL
    field = message.field.add()
    field.name = "message"
    field.number = 2
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING

    message = file_proto.message_type.add()
    message.name = "LatestImageRequest"

    message = file_proto.message_type.add()
    message.name = "LatestImageReply"
    field = message.field.add()
    field.name = "has_image"
    field.number = 1
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_BOOL
    field = message.field.add()
    field.name = "filename"
    field.number = 2
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "timestamp_ms"
    field.number = 3
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
    field = message.field.add()
    field.name = "source"
    field.number = 4
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "data"
    field.number = 5
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_BYTES
    field = message.field.add()
    field.name = "sequence"
    field.number = 6
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64

    message = file_proto.message_type.add()
    message.name = "TrackDetectionProto"
    field = message.field.add()
    field.name = "x"
    field.number = 1
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE
    field = message.field.add()
    field.name = "y"
    field.number = 2
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE
    field = message.field.add()
    field.name = "mass"
    field.number = 3
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE
    field = message.field.add()
    field.name = "ecc"
    field.number = 4
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE
    field = message.field.add()
    field.name = "size"
    field.number = 5
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE
    field = message.field.add()
    field.name = "signal"
    field.number = 6
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE

    message = file_proto.message_type.add()
    message.name = "FrameMetadata"
    field = message.field.add()
    field.name = "filename"
    field.number = 1
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "sequence"
    field.number = 2
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
    field = message.field.add()
    field.name = "timestamp_ms"
    field.number = 3
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
    field = message.field.add()
    field.name = "source"
    field.number = 4
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "image_width"
    field.number = 5
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
    field = message.field.add()
    field.name = "image_height"
    field.number = 6
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
    field = message.field.add()
    field.name = "latency_ms"
    field.number = 7
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
    field = message.field.add()
    field.name = "processing_ms"
    field.number = 8
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
    field = message.field.add()
    field.name = "storage_ms"
    field.number = 9
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
    field = message.field.add()
    field.name = "storage_saved"
    field.number = 10
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_BOOL
    field = message.field.add()
    field.name = "storage_kind"
    field.number = 11
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "storage_path"
    field.number = 12
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "storage_codec"
    field.number = 13
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "storage_ratio"
    field.number = 14
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE
    field = message.field.add()
    field.name = "storage_bytes_in"
    field.number = 15
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
    field = message.field.add()
    field.name = "storage_bytes_out"
    field.number = 16
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT64
    field = message.field.add()
    field.name = "storage_throttle_ms"
    field.number = 17
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE
    field = message.field.add()
    field.name = "storage_message"
    field.number = 18
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING

    message = file_proto.message_type.add()
    message.name = "FrameEnvelope"
    field = message.field.add()
    field.name = "metadata"
    field.number = 1
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
    field.type_name = ".images.FrameMetadata"
    field = message.field.add()
    field.name = "image_data"
    field.number = 2
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_BYTES
    field = message.field.add()
    field.name = "image_format"
    field.number = 3
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "detections"
    field.number = 4
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
    field.type_name = ".images.TrackDetectionProto"
    field = message.field.add()
    field.name = "detection_count"
    field.number = 5
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
    field = message.field.add()
    field.name = "has_tracks"
    field.number = 6
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_BOOL
    field = message.field.add()
    field.name = "tracking_config_snapshot"
    field.number = 7
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
    field.type_name = ".google.protobuf.Struct"

    message = file_proto.message_type.add()
    message.name = "StorageConfig"
    field = message.field.add()
    field.name = "enabled"
    field.number = 1
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_BOOL
    field = message.field.add()
    field.name = "target_fps"
    field.number = 2
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE
    field = message.field.add()
    field.name = "image_format"
    field.number = 3
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "output_dir"
    field.number = 4
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "hdf5_enabled"
    field.number = 5
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_BOOL
    field = message.field.add()
    field.name = "hdf5_path"
    field.number = 6
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "tiff_compression"
    field.number = 7
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING
    field = message.field.add()
    field.name = "tiff_compression_level"
    field.number = 8
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
    field = message.field.add()
    field.name = "png_compression_level"
    field.number = 9
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_INT32
    field = message.field.add()
    field.name = "bit_depth"
    field.number = 10
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_STRING

    service = file_proto.service.add()
    service.name = "ImageExchange"
    method = service.method.add()
    method.name = "UploadImage"
    method.input_type = ".images.ImageChunk"
    method.output_type = ".images.UploadAck"
    method = service.method.add()
    method.name = "GetLatestImage"
    method.input_type = ".images.LatestImageRequest"
    method.output_type = ".images.LatestImageReply"
    method = service.method.add()
    method.name = "GetLatestTracks"
    method.input_type = ".google.protobuf.Empty"
    method.output_type = ".google.protobuf.Struct"
    method = service.method.add()
    method.name = "GetTrackingConfig"
    method.input_type = ".google.protobuf.Empty"
    method.output_type = ".google.protobuf.Struct"
    method = service.method.add()
    method.name = "UpdateTrackingConfig"
    method.input_type = ".google.protobuf.Struct"
    method.output_type = ".google.protobuf.Struct"
    method = service.method.add()
    method.name = "StreamFrames"
    method.input_type = ".google.protobuf.Empty"
    method.output_type = ".images.FrameEnvelope"
    method.client_streaming = False
    method.server_streaming = True
    method = service.method.add()
    method.name = "GetStorageConfig"
    method.input_type = ".google.protobuf.Empty"
    method.output_type = ".images.StorageConfig"
    method = service.method.add()
    method.name = "UpdateStorageConfig"
    method.input_type = ".images.StorageConfig"
    method.output_type = ".images.StorageConfig"

    _pool.Add(file_proto)


def _load_generated() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    try:
        import image_exchange_pb2 as pb2  # type: ignore
    except ImportError:
        try:
            from . import image_exchange_pb2 as pb2  # type: ignore
        except ImportError as exc:
            raise RuntimeError("generated protobufs are missing") from exc
    chunk_fields = {field.name for field in pb2.ImageChunk.DESCRIPTOR.fields}  # type: ignore[attr-defined]
    reply_fields = {field.name for field in pb2.LatestImageReply.DESCRIPTOR.fields}  # type: ignore[attr-defined]
    metadata_fields = {field.name for field in getattr(pb2, "FrameMetadata").DESCRIPTOR.fields}  # type: ignore[attr-defined]
    storage_fields = {field.name for field in getattr(pb2, "StorageConfig").DESCRIPTOR.fields}  # type: ignore[attr-defined]
    required_metadata = {
        "storage_ms",
        "storage_saved",
        "storage_kind",
        "storage_path",
        "storage_codec",
        "storage_ratio",
        "storage_bytes_in",
        "storage_bytes_out",
        "storage_throttle_ms",
        "storage_message",
    }
    required_storage = {
        "hdf5_enabled",
        "hdf5_path",
        "tiff_compression",
        "tiff_compression_level",
        "png_compression_level",
        "bit_depth",
    }
    if (
        "sequence" not in chunk_fields
        or "sequence" not in reply_fields
        or not required_metadata.issubset(metadata_fields)
        or not required_storage.issubset(storage_fields)
    ):
        raise RuntimeError("generated protobufs are outdated")
    return (
        pb2.ImageChunk,  # type: ignore[attr-defined]
        pb2.UploadAck,  # type: ignore[attr-defined]
        pb2.LatestImageRequest,  # type: ignore[attr-defined]
        pb2.LatestImageReply,  # type: ignore[attr-defined]
        getattr(pb2, "FrameEnvelope"),  # type: ignore[attr-defined]
        getattr(pb2, "StorageConfig"),  # type: ignore[attr-defined]
        getattr(pb2, "TrackDetectionProto"),  # type: ignore[attr-defined]
    )


def _load_dynamic() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    ensure_registered()

    image_chunk_desc = _pool.FindMessageTypeByName("images.ImageChunk")
    upload_ack_desc = _pool.FindMessageTypeByName("images.UploadAck")
    latest_req_desc = _pool.FindMessageTypeByName("images.LatestImageRequest")
    latest_reply_desc = _pool.FindMessageTypeByName("images.LatestImageReply")
    frame_envelope_desc = _pool.FindMessageTypeByName("images.FrameEnvelope")
    storage_config_desc = _pool.FindMessageTypeByName("images.StorageConfig")
    detection_desc = _pool.FindMessageTypeByName("images.TrackDetectionProto")

    try:
        get_cls = _factory.GetPrototype  # type: ignore[attr-defined]
    except AttributeError:
        get_cls = message_factory.GetMessageClass  # type: ignore[attr-defined]

    image_chunk_cls = get_cls(image_chunk_desc)  # type: ignore[misc]
    upload_ack_cls = get_cls(upload_ack_desc)  # type: ignore[misc]
    latest_req_cls = get_cls(latest_req_desc)  # type: ignore[misc]
    latest_reply_cls = get_cls(latest_reply_desc)  # type: ignore[misc]
    frame_envelope_cls = get_cls(frame_envelope_desc)  # type: ignore[misc]
    storage_config_cls = get_cls(storage_config_desc)  # type: ignore[misc]
    detection_cls = get_cls(detection_desc)  # type: ignore[misc]

    _sym_db.RegisterMessage(image_chunk_cls)
    _sym_db.RegisterMessage(upload_ack_cls)
    _sym_db.RegisterMessage(latest_req_cls)
    _sym_db.RegisterMessage(latest_reply_cls)
    _sym_db.RegisterMessage(frame_envelope_cls)
    _sym_db.RegisterMessage(storage_config_cls)
    _sym_db.RegisterMessage(detection_cls)

    return (
        image_chunk_cls,
        upload_ack_cls,
        latest_req_cls,
        latest_reply_cls,
        frame_envelope_cls,
        storage_config_cls,
        detection_cls,
    )


try:
    (
        ImageChunk,
        UploadAck,
        LatestImageRequest,
        LatestImageReply,
        FrameEnvelope,
        StorageConfig,
        TrackDetectionProto,
    ) = _load_generated()
except RuntimeError:
    (
        ImageChunk,
        UploadAck,
        LatestImageRequest,
        LatestImageReply,
        FrameEnvelope,
        StorageConfig,
        TrackDetectionProto,
    ) = _load_dynamic()