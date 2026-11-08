from pathlib import Path
from typing import Mapping

import bpy

from .bridge import get_client
from .catalog import MODELS
from .properties import get_models_directory, get_preferences
from .ui_helpers import (
    ACTIVE_STATUSES,
    clamp_progress,
    find_job,
    find_model_state,
    format_bytes,
    has_active_jobs,
    normalize_status,
    read_field,
    resource_metric,
    result_path,
    status_icon,
    status_label,
    storage_label,
    validate_image_path,
)


_SNAPSHOT = {}
_RUNTIME_ERROR = ""


def runtime_snapshot():
    return _SNAPSHOT


def _tag_view3d_redraw():
    window_manager = getattr(bpy.context, "window_manager", None)
    if window_manager is None:
        return
    for window in window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _poll_runtime():
    global _RUNTIME_ERROR, _SNAPSHOT
    try:
        snapshot = get_client(bpy.context).snapshot() or {}
        if not isinstance(snapshot, Mapping):
            raise TypeError("Engine returned an invalid status snapshot")
        snapshot = dict(snapshot)
        error = ""
    except Exception as exception:
        snapshot = _SNAPSHOT
        error = str(exception)
    if snapshot != _SNAPSHOT or error != _RUNTIME_ERROR:
        _SNAPSHOT = snapshot
        _RUNTIME_ERROR = error
        _tag_view3d_redraw()
    return 0.5


def start_runtime_poll():
    if not bpy.app.timers.is_registered(_poll_runtime):
        bpy.app.timers.register(_poll_runtime, first_interval=0.2, persistent=True)


def stop_runtime_poll():
    if bpy.app.timers.is_registered(_poll_runtime):
        bpy.app.timers.unregister(_poll_runtime)


def _catalog_models():
    if isinstance(MODELS, Mapping):
        return tuple(MODELS.items())
    models = []
    for model in MODELS:
        model_id = str(read_field(model, "id", read_field(model, "model_id", "")))
        models.append((model_id, model))
    return tuple(models)


def _model_name(model):
    return str(read_field(model, "name", read_field(model, "display_name", "Model")))


def _model_size(model):
    display_size = read_field(model, "display_size")
    if display_size:
        return str(display_size)
    size = read_field(
        model,
        "size_bytes",
        read_field(model, "selected_size", read_field(model, "download_size")),
    )
    return format_bytes(size)


def _model_requires_license(model_id, model):
    explicit = read_field(model, "requires_license", read_field(model, "restricted"))
    if explicit is not None:
        return bool(explicit)
    repository = str(read_field(model, "repository", read_field(model, "repo_id", ""))).lower()
    return repository.startswith("tencent/") or model_id in {
        "hunyuan",
        "hunyuan_mv",
        "hunyuan_more_views",
        "enhance_texture",
    }


def _model_id_containing(text):
    text = text.lower()
    for model_id, model in _catalog_models():
        if text in _model_name(model).lower() or text in model_id.lower():
            return model_id
    return ""


def _model_installed(model_id):
    return bool(model_id) and normalize_status(find_model_state(_SNAPSHOT, model_id).get("status")) == "installed"


def _draw_job_card(layout, job_id, title):
    if not job_id:
        return
    job = find_job(_SNAPSHOT, job_id)
    status = normalize_status(job.get("status", "queued"))
    progress = clamp_progress(job.get("progress", job.get("percent", 0.0)))
    detail = str(job.get("message", job.get("detail", job.get("error", ""))))

    box = layout.box()
    header = box.row()
    header.label(text=title)
    header.label(text=status_label(status, progress), icon=status_icon(status))
    if status in ACTIVE_STATUSES:
        box.progress(factor=progress, text=f"{progress:.0%}")
        cancel = box.operator("sculptr.cancel_job", text="Cancel", icon="CANCEL")
        cancel.job_id = job_id
    elif status == "completed":
        output = result_path(job)
        if output:
            action = box.operator("sculptr.import_result", text="Import Result", icon="IMPORT")
            action.filepath = output
    if detail:
        detail_row = box.row()
        detail_row.alert = status == "failed"
        detail_row.label(text=detail, icon="ERROR" if status == "failed" else "INFO")


class _SculptRPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "SculptR"


class _SculptRChildPanel(_SculptRPanel):
    bl_parent_id = "SCULPTR_PT_root"


class SCULPTR_PT_root(_SculptRPanel, bpy.types.Panel):
    bl_idname = "SCULPTR_PT_root"
    bl_label = "SculptR"

    def draw(self, context):
        layout = self.layout
        layout.prop(context.scene.sculptr, "active_tab", expand=True)
        if _RUNTIME_ERROR:
            error = layout.box()
            error.alert = True
            error.label(text=_RUNTIME_ERROR, icon="ERROR")


class SCULPTR_PT_image(_SculptRChildPanel, bpy.types.Panel):
    bl_idname = "SCULPTR_PT_image"
    bl_label = "Image"
    bl_order = 0

    @classmethod
    def poll(cls, context):
        return context.scene.sculptr.active_tab == "GENERATE"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sculptr
        main = layout.box()
        row = main.row(align=True)
        choose = row.operator(
            "sculptr.choose_image",
            text=Path(settings.main_image).name if settings.main_image else "Choose Image",
            icon="IMAGE_DATA",
        )
        choose.slot = "main"
        if settings.main_image:
            clear = row.operator("sculptr.clear_image", text="", icon="X")
            clear.slot = "main"
        main.label(text="Required main/front view")

        if settings.main_image:
            views = layout.row(align=True)
            for slot, label in (("left", "Left"), ("right", "Right"), ("back", "Back")):
                path = getattr(settings, f"{slot}_image")
                column = views.column(align=True)
                box = column.box()
                box.label(text=label)
                button = box.operator(
                    "sculptr.choose_image",
                    text=Path(path).name if path else "Choose",
                    icon="IMAGE_DATA",
                )
                button.slot = slot
                if path:
                    clear = box.operator("sculptr.clear_image", text="Clear", icon="X")
                    clear.slot = slot
        layout.label(text="Drop images anywhere in the 3D View", icon="IMPORT")


class SCULPTR_PT_resources(_SculptRChildPanel, bpy.types.Panel):
    bl_idname = "SCULPTR_PT_resources"
    bl_label = "Resources"
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return context.scene.sculptr.active_tab == "GENERATE"

    def draw(self, context):
        layout = self.layout
        for resource, label in (("vram", "VRAM"), ("ram", "RAM"), ("cpu", "CPU"), ("gpu", "GPU")):
            factor, details = resource_metric(_SNAPSHOT, resource)
            row = layout.row(align=True)
            label_column = row.column()
            label_column.ui_units_x = 4
            label_column.label(text=label)
            bar = row.column()
            bar.progress(factor=factor, text="")
            detail_column = row.column()
            detail_column.ui_units_x = 7
            detail_column.label(text=details)
        layout.separator()
        layout.operator("sculptr.clear_cache", icon="TRASH")
        _draw_job_card(layout, context.scene.sculptr.utility_job_id, "Cache")


class SCULPTR_PT_settings(_SculptRChildPanel, bpy.types.Panel):
    bl_idname = "SCULPTR_PT_settings"
    bl_label = "Settings"
    bl_order = 2

    @classmethod
    def poll(cls, context):
        return context.scene.sculptr.active_tab == "GENERATE"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sculptr
        layout.label(text="Quality")
        layout.prop(settings, "quality", expand=True)

        layout.label(text="Faces")
        faces = layout.row(align=True)
        slider = faces.row(align=True)
        slider.prop(settings, "faces", text="", slider=True)
        number = faces.row(align=True)
        number.ui_units_x = 6
        number.prop(settings, "faces", text="")

        layout.prop(settings, "use_texture", toggle=True)
        if settings.use_texture:
            layout.prop(settings, "texture_size", expand=True)
        layout.prop(settings, "seed", slider=True)

        valid_main, _ = validate_image_path(settings.main_image)
        current_job = find_job(_SNAPSHOT, settings.generation_job_id)
        running = normalize_status(current_job.get("status")) in ACTIVE_STATUSES
        multiview = any((settings.left_image, settings.right_image, settings.back_image))
        shape_ready = _model_installed(
            _model_id_containing("Hunyuan More Views" if multiview else "Hunyuan")
        )
        texture_ready = not settings.use_texture or _model_installed(
            _model_id_containing("Enhance Texture")
        )
        generate = layout.column()
        generate.enabled = valid_main and shape_ready and texture_ready and not running
        generate.scale_y = 1.4
        generate.operator("sculptr.generate", icon="OUTLINER_OB_MESH")
        if not valid_main:
            layout.label(text="Choose a valid main image to generate", icon="INFO")
        elif not shape_ready or not texture_ready:
            layout.label(text="Install and check the required models", icon="INFO")
        _draw_job_card(layout, settings.generation_job_id, "Generation")


class SCULPTR_PT_enhance(_SculptRChildPanel, bpy.types.Panel):
    bl_idname = "SCULPTR_PT_enhance"
    bl_label = "Enhance"
    bl_order = 3

    @classmethod
    def poll(cls, context):
        return context.scene.sculptr.active_tab == "GENERATE"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sculptr
        active_mesh = bool(context.active_object and context.active_object.type == "MESH")
        valid_main = validate_image_path(settings.main_image)[0]
        job = find_job(_SNAPSHOT, settings.enhance_job_id)
        running = normalize_status(job.get("status")) in ACTIVE_STATUSES
        texture_ready = _model_installed(_model_id_containing("Enhance Texture"))
        detail_ready = _model_installed(_model_id_containing("Hi3D"))

        actions = layout.row(align=True)
        repaint_column = actions.column()
        repaint_column.enabled = active_mesh and valid_main and texture_ready and not running
        repaint = repaint_column.operator("sculptr.enhance", text="Repaint Texture", icon="TEXTURE")
        repaint.action = "repaint_texture"
        detail_column = actions.column()
        detail_column.enabled = active_mesh and valid_main and detail_ready and not running
        detail = detail_column.operator("sculptr.enhance", text="Enhance Detail", icon="MOD_REMESH")
        detail.action = "enhance_detail"
        undo = layout.row()
        undo.enabled = active_mesh and not running
        undo.operator("sculptr.undo", icon="LOOP_BACK")

        if not active_mesh:
            layout.label(text="Select a mesh to enhance", icon="INFO")
        elif not valid_main:
            layout.label(text="Choose a valid main image to enhance", icon="INFO")
        elif not texture_ready or not detail_ready:
            layout.label(text="Install required enhancement models", icon="INFO")
        _draw_job_card(layout, settings.enhance_job_id, "Enhancement")


class SCULPTR_PT_folder(_SculptRChildPanel, bpy.types.Panel):
    bl_idname = "SCULPTR_PT_folder"
    bl_label = "Folder"
    bl_order = 0

    @classmethod
    def poll(cls, context):
        return context.scene.sculptr.active_tab == "MODELS"

    def draw(self, context):
        layout = self.layout
        preferences = get_preferences(context)
        path = get_models_directory(context)
        path_row = layout.row()
        path_row.enabled = False
        path_row.prop(preferences, "models_directory", text="", icon="FILE_FOLDER")
        layout.label(text=storage_label(_SNAPSHOT))

        buttons = layout.row(align=True)
        change = buttons.column()
        change.enabled = not has_active_jobs(_SNAPSHOT)
        change.operator("sculptr.change_models_folder", text="Change", icon="FILE_FOLDER")
        open_folder = buttons.column()
        open_folder.enabled = path.is_dir()
        open_folder.operator("sculptr.open_models_folder", text="Open in Explorer", icon="DISCLOSURE_TRI_RIGHT")


class SCULPTR_PT_models(_SculptRChildPanel, bpy.types.Panel):
    bl_idname = "SCULPTR_PT_models"
    bl_label = "Models"
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return context.scene.sculptr.active_tab == "MODELS"

    def draw(self, context):
        layout = self.layout
        preferences = get_preferences(context)

        license_box = layout.box()
        license_box.alert = not preferences.hunyuan_license_confirmed
        license_box.label(text="US deployment: review Hunyuan license", icon="ERROR")
        license_box.prop(
            preferences,
            "hunyuan_license_confirmed",
            text="I accept the applicable Hunyuan license",
        )

        for model_id, model in _catalog_models():
            state = find_model_state(_SNAPSHOT, model_id)
            status = normalize_status(state.get("status"))
            progress = clamp_progress(state.get("progress", state.get("percent", 0.0)))
            detail = str(state.get("message", state.get("detail", state.get("error", ""))))
            job_id = str(state.get("job_id", state.get("id", "")))

            card = layout.box()
            title = card.row()
            title.label(text=_model_name(model))
            title.label(text=_model_size(model))
            status_row = card.row()
            status_row.alert = status == "failed"
            status_row.label(text=status_label(status, progress, detail), icon=status_icon(status))
            if status in {"checking", "downloading"}:
                card.progress(factor=progress, text=f"{progress:.0%}")

            actions = card.row(align=True)
            primary = actions.column()
            restricted = _model_requires_license(model_id, model)
            if status == "downloading":
                primary.enabled = bool(job_id)
                operator = primary.operator("sculptr.cancel_job", text="Cancel", icon="CANCEL")
                operator.job_id = job_id
            elif status in {"failed", "cancelled"}:
                primary.enabled = not restricted or preferences.hunyuan_license_confirmed
                operator = primary.operator("sculptr.model_action", text="Retry", icon="FILE_REFRESH")
                operator.action = "retry"
                operator.model_id = model_id
            elif status == "installed":
                primary.enabled = False
                primary.operator("sculptr.model_action", text="Installed", icon="CHECKMARK")
            else:
                primary.enabled = (
                    status not in ACTIVE_STATUSES
                    and (not restricted or preferences.hunyuan_license_confirmed)
                )
                operator = primary.operator("sculptr.model_action", text="Download", icon="IMPORT")
                operator.action = "download"
                operator.model_id = model_id

            check = actions.column()
            check.enabled = status not in ACTIVE_STATUSES
            operator = check.operator("sculptr.model_action", text="Check", icon="FILE_TICK")
            operator.action = "check"
            operator.model_id = model_id


CLASSES = (
    SCULPTR_PT_root,
    SCULPTR_PT_image,
    SCULPTR_PT_resources,
    SCULPTR_PT_settings,
    SCULPTR_PT_enhance,
    SCULPTR_PT_folder,
    SCULPTR_PT_models,
)
