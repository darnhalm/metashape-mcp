"""Validation for explicit, copy-based sensor calibration changes."""

SENSOR_TYPES = {"frame": "Frame", "fisheye": "Fisheye", "spherical": "Spherical",
                "cylindrical": "Cylindrical"}
CALIBRATION_PARAMETERS = {
    "f": "F", "cx": "Cx", "cy": "Cy", "b1": "B1", "b2": "B2",
    "k1": "K1", "k2": "K2", "k3": "K3", "k4": "K4",
    "p1": "P1", "p2": "P2",
}


def validate_sensor_calibration_plan(sensor_key, calibration_path=None, sensor_type=None,
                                     fixed=None, fixed_params=None):
    if isinstance(sensor_key, bool) or not isinstance(sensor_key, int) or sensor_key < 0:
        raise ValueError("sensor_key должен быть неотрицательным целым числом.")
    if calibration_path is not None and (
            not isinstance(calibration_path, str) or not calibration_path.strip()):
        raise ValueError("calibration_path должен быть непустым путём или null.")
    if sensor_type is not None and sensor_type not in SENSOR_TYPES:
        raise ValueError(f"sensor_type: допустимы {', '.join(SENSOR_TYPES)}.")
    if fixed is not None and not isinstance(fixed, bool):
        raise ValueError("fixed должен быть true, false или null.")
    if fixed_params is not None:
        if not isinstance(fixed_params, list) or any(not isinstance(value, str)
                                                     for value in fixed_params):
            raise ValueError("fixed_params должен быть списком имён параметров или null.")
        if len(set(fixed_params)) != len(fixed_params):
            raise ValueError("fixed_params содержит повторы.")
        normalized = [value.lower() for value in fixed_params]
        unknown = sorted(set(normalized) - set(CALIBRATION_PARAMETERS))
        if unknown:
            raise ValueError(f"Неизвестные параметры калибровки: {unknown}.")
        fixed_params = [CALIBRATION_PARAMETERS[value] for value in normalized]
    if fixed is True and fixed_params:
        raise ValueError("fixed=true фиксирует всю калибровку; не задавайте одновременно "
                         "непустой fixed_params.")
    if all(value is None for value in (calibration_path, sensor_type, fixed, fixed_params)):
        raise ValueError("Укажите хотя бы одно изменение калибровки сенсора.")
    return {
        "sensor_key": sensor_key,
        "calibration_path": calibration_path,
        "sensor_type": sensor_type,
        "fixed": fixed,
        "fixed_params": fixed_params,
    }
