"""Validation and stable names for gradual tie-point filtering plans."""

CRITERIA = {
    "reconstruction_uncertainty": "ReconstructionUncertainty",
    "projection_accuracy": "ProjectionAccuracy",
    "reprojection_error": "ReprojectionError",
}

DEFAULT_MAX_SELECTED_FRACTION = 0.30
MAX_PASSES = 6


def validate_passes(passes):
    if not isinstance(passes, list) or not passes:
        raise ValueError("passes должен содержать хотя бы один проход фильтрации.")
    if len(passes) > MAX_PASSES:
        raise ValueError(f"Допустимо не больше {MAX_PASSES} проходов за одно задание.")
    result = []
    for index, item in enumerate(passes):
        if not isinstance(item, dict):
            raise ValueError(f"passes[{index}] должен быть объектом.")
        unknown = set(item) - {"criterion", "threshold", "optimize"}
        if unknown:
            raise ValueError(f"passes[{index}]: неизвестные поля: {', '.join(sorted(unknown))}.")
        criterion = item.get("criterion")
        if criterion not in CRITERIA:
            raise ValueError(f"passes[{index}].criterion: допустимы {', '.join(CRITERIA)}.")
        threshold = item.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError(f"passes[{index}].threshold должен быть числом.")
        threshold = float(threshold)
        if not 0 < threshold < 1_000_000:
            raise ValueError(f"passes[{index}].threshold должен быть больше 0.")
        optimize = item.get("optimize", True)
        if not isinstance(optimize, bool):
            raise ValueError(f"passes[{index}].optimize должен быть true или false.")
        result.append({"criterion": criterion, "threshold": threshold, "optimize": optimize})
    return result


def validate_max_selected_fraction(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("max_selected_fraction должен быть числом.")
    value = float(value)
    if not 0 < value <= 0.90:
        raise ValueError("max_selected_fraction должен быть больше 0 и не больше 0.90.")
    return value


def optimization_parameters(fit_principal_point=True, fit_skew=False, adaptive_fitting=False):
    for name, value in (("fit_principal_point", fit_principal_point), ("fit_skew", fit_skew),
                        ("adaptive_fitting", adaptive_fitting)):
        if not isinstance(value, bool):
            raise ValueError(f"{name} должен быть true или false.")
    return {
        "fit_f": True, "fit_cx": fit_principal_point, "fit_cy": fit_principal_point,
        "fit_b1": fit_skew, "fit_b2": fit_skew,
        "fit_k1": True, "fit_k2": True, "fit_k3": True, "fit_k4": False,
        "fit_p1": True, "fit_p2": True, "fit_corrections": False,
        "adaptive_fitting": adaptive_fitting, "tiepoint_covariance": False,
    }
