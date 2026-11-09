import pytest

from surogates.devices.operations import DeviceOperations


def test_patience_without_notice_is_refused():
    with pytest.raises(ValueError, match="patience needs a notice"):
        DeviceOperations(object(), object(), patience=object())
