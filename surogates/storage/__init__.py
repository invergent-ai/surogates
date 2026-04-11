"""Storage abstraction layer.

Provides a ``StorageBackend`` protocol with two implementations:

- ``LocalBackend`` — maps buckets to directories on the local filesystem.
  Used for development and single-node deployments.
- ``S3Backend`` — uses an S3-compatible API (Garage, MinIO, AWS S3) for distributed
  deployments.  Requires ``aioboto3``.

The backend is selected via ``StorageSettings.backend`` in the app config.
"""
