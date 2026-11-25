"""Conservative, read-only workflow guidance exposed through MCP resources/prompts."""

from copy import deepcopy


VERSION = "0.46.0"


_SAFE_WORKFLOW_CATALOG = {
    "version": VERSION,
    "purpose": "Plan Metashape work without bypassing typed tools, review or rollback.",
    "principles": [
        "Inspect the live project before choosing parameters.",
        "Do not infer CRS, units, scale, accuracy, thresholds or output quality from a file name.",
        "For copy-based jobs use prepare, review the plan and copy estimate, start, wait, then explicitly accept or reject.",
        "Prefer the source chunk until the result copy has been measured or rendered and accepted.",
        "Write exports only into a fresh registered directory; generated outputs are tracked for reversible quarantine.",
        "Run user scripts only after explicit user intent, UI opt-in, hash review and an offered project save.",
    ],
    "lifecycles": {
        "inspect": ["preflight", "get_project_summary", "list_chunks", "list_chunk_assets"],
        "copy_job": [
            "prepare_*",
            "review warnings, fingerprint and copy_estimate",
            "start_*",
            "wait_job or get_job_status",
            "inspect result metrics or renders",
            "accept_job_result or reject_job_result",
        ],
        "export": [
            "inspect the exact asset and CRS",
            "export_* to a new directory under an allowed root",
            "verify returned files and sizes",
            "keep the restore point until the output is accepted",
        ],
        "user_script": [
            "explicit user request",
            "offer save_project",
            "UI setting enabled with warning",
            "review file path and SHA-256",
            "run_user_script",
            "review persistent log and external side effects",
        ],
    },
    "workflows": {
        "object": {
            "prompt": "metashape_object_workflow",
            "typical_outputs": ["textured model", "GLB/OBJ/PLY/STL/FBX", "PDF report"],
        },
        "aerial": {
            "prompt": "metashape_aerial_workflow",
            "typical_outputs": ["point cloud", "DEM", "orthomosaic", "accuracy report"],
        },
        "gis_quarry": {
            "prompt": "metashape_gis_quarry_workflow",
            "typical_outputs": ["measurements", "contours", "rasters", "3D Tiles/SLPK/OSGB"],
        },
        "diagnose_alignment": {
            "prompt": "metashape_diagnose_alignment",
            "typical_outputs": ["evidence summary", "minimal repair plan", "before/after checks"],
        },
        "batch_export": {
            "prompt": "metashape_batch_export",
            "typical_outputs": ["confirmed export manifest", "verified files and sizes"],
        },
        "quality_settings": {
            "prompt": "metashape_quality_settings",
            "typical_outputs": ["draft profile", "balanced profile", "detailed profile"],
        },
    },
    "limits": [
        "Prompts only compose a plan; they do not execute tools.",
        "Existing shapes can be measured read-only, but safe shape creation/editing is not exposed yet.",
        "XMP/RTK orientation and vertical/geoid workflows need representative data before acceptance.",
        "User-script changes outside the Metashape project may not be reversible.",
    ],
}


def safe_workflow_catalog() -> dict[str, object]:
    """Return a copy so callers cannot mutate the module-level catalog."""
    return deepcopy(_SAFE_WORKFLOW_CATALOG)


def _header(title: str, goal: str, notes: str) -> list[str]:
    lines = [
        f"Составь консервативный план работы в Metashape: {title}.",
        f"Цель пользователя: {goal.strip() or 'не указана — сначала уточни результат'}.",
    ]
    if notes.strip():
        lines.append(f"Дополнительные условия: {notes.strip()}.")
    lines.extend([
        "Это шаблон планирования: не вызывай инструменты автоматически и не считай согласие на план разрешением на изменения.",
        "Начни с preflight, get_project_summary, list_chunks и list_chunk_assets для выбранного чанка.",
        "Не угадывай CRS, единицы, масштаб, точность, пороги и качество; покажи найденные факты и запроси недостающее.",
    ])
    return lines


def _safety_tail() -> list[str]:
    return [
        "Для каждого изменяющего задания используй prepare_* → проверка предупреждений, fingerprint и copy_estimate → start_* → wait_job/get_job_status.",
        "После выполнения проверь метрики или рендеры копии. Не вызывай accept_job_result без явного решения пользователя; предложи accept_job_result или reject_job_result.",
        "Экспортируй только в новую папку под разрешённым корнем, затем проверь список и размеры файлов; сохраняй возможность карантина через restore point.",
        "Не вызывай run_user_script без отдельного прямого запроса, включённой пользователем настройки, проверки пути/SHA-256 и предложения сначала выполнить save_project.",
        "В ответе перечисли: исходные факты, вопросы, последовательность инструментов, точки принятия решения, проверку результата и способ отката.",
    ]


def object_workflow_prompt(goal: str = "текстурированная 3D-модель", notes: str = "") -> str:
    lines = _header("предметная съёмка", goal, notes)
    lines.extend([
        "Проверь маски, качество и выравнивание камер; исключение кадров или пороги предлагай только по измеренным данным.",
        "Планируй выравнивание и реконструкцию по этапам либо через описанный autopilot на рабочей копии.",
        "Перед доработкой модели прочитай get_model_statistics; после изменения геометрии строй UV/текстуру заново.",
        "Сравни число граней, компоненты, открытые края, топологию, текстуры и контрольные рендеры до принятия копии.",
        "Выбери формат export_model по назначению, а не по умолчанию; при необходимости добавь export_report.",
    ])
    lines.extend(_safety_tail())
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1))


def aerial_workflow_prompt(goal: str = "ЦМР и ортомозаика", notes: str = "") -> str:
    lines = _header("аэросъёмка", goal, notes)
    lines.extend([
        "Проверь EXIF/XMP, систему координат камер/маркеров/чанка, точности, GCP и независимые check points; не выдавай GPS EXIF за RTK.",
        "Спланируй выравнивание, постепенную фильтрацию tie points и оптимизацию камер с измеримыми лимитами удаления.",
        "Затем планируй depth maps/point cloud, при необходимости классификацию земли, DEM и orthomosaic; не подменяй исходный чанк до проверки.",
        "Оцени ошибки камер, GCP и отдельно check points; если check points отсутствуют, прямо укажи, что независимая точность не подтверждена.",
        "Для raster/export_report укажи явные CRS, разрешение, границы и формат, подтверждённые пользователем или проектом.",
    ])
    lines.extend(_safety_tail())
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1))


def gis_quarry_workflow_prompt(goal: str = "измерения и экспорт", notes: str = "") -> str:
    lines = _header("ГИС / карьер", goal, notes)
    lines.extend([
        "Проверь горизонтальную и вертикальную CRS, геоид, масштаб и единицы; не преобразуй координаты без явных source/target EPSG и проверки контрольных точек.",
        "Инвентаризируй point clouds, DEM, orthomosaics, tiled models и существующие shapes по точным ключам.",
        "Для существующих фигур используй get_shape_measurements и всегда передавай пользователю CRS, единицы и предупреждения вместе с площадью/периметром/объёмом.",
        "Безопасное создание и редактирование shapes пока не предоставлено: не заменяй это run_user_script. Если оно нужно, обозначь как отдельный будущий блок.",
        "Контуры и raster products строй на копии; tiled model экспортируй через export_tiled_model с явным форматом и CRS-ограничениями.",
        "Для объёмов и сравнения эпох проверь одинаковые CRS, datum, единицы, границы и опорную поверхность; не интерпретируй условные chunk units как метры.",
    ])
    lines.extend(_safety_tail())
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1))


def alignment_diagnostic_prompt(goal: str = "найти причину плохого выравнивания",
                                notes: str = "") -> str:
    lines = _header("диагностика выравнивания", goal, notes)
    lines.extend([
        "Прочитай get_alignment_diagnostics, get_reference_error_statistics, list_sensors и маски; отделяй невыровненные камеры, относительные разрывы маршрута, tie-point RMS и ошибки GCP/check points.",
        "Не объявляй относительный разрыв дрейфом и не применяй универсальные пороги: порядок камер может не совпадать со временем съёмки.",
        "Сначала предложи читающие проверки и только затем минимальное исправление на копии: маски/камеры, повторное matching/alignment, постепенная фильтрация или оптимизация.",
        "После исправления сравни число выровненных камер, tie points, reprojection RMS, GCP и независимые check points с исходным чанком.",
    ])
    lines.extend(_safety_tail())
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1))


def batch_export_prompt(goal: str = "экспортировать готовые результаты", notes: str = "") -> str:
    lines = _header("пакетный экспорт", goal, notes)
    lines.extend([
        "Инвентаризируй точные ключи моделей, облаков, DEM, orthomosaic и tiled models; отсутствующие продукты не строй без отдельного решения.",
        "Составь таблицу продукт → формат → CRS → разрешение/классы/текстуры → новая папка назначения и запроси подтверждение спорных полей.",
        "Для каждого export_* проверь возвращённые файлы и размеры. Не считай успешный ответ доказательством экспорта без файлов.",
        "Не объединяй результаты разных CRS и не перезаписывай существующие каталоги; сохраняй restore point до приёмки всех файлов.",
    ])
    lines.extend(_safety_tail())
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1))


def quality_settings_prompt(goal: str = "подобрать качество и оценить ресурсы", notes: str = "") -> str:
    lines = _header("настройки качества", goal, notes)
    lines.extend([
        "Собери число и разрешение снимков, типы сенсоров, маски, текущие активы, область обработки, свободный диск и доступные сведения о CPU/GPU/RAM.",
        "Не обещай время и память без измерений. Дай несколько профилей (черновой/сбалансированный/детальный) с параметрами и явными компромиссами.",
        "Сначала предложи малую проверку или один этап на рабочей копии; используй фактическое время, размер активов и качество результата для уточнения следующего этапа.",
        "Не повышай качество только потому, что оно доступно: привяжи downscale, face count, texture size и resolution к требуемому выходу.",
    ])
    lines.extend(_safety_tail())
    return "\n".join(f"{index}. {line}" for index, line in enumerate(lines, 1))
