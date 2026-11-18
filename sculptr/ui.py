import time
from pathlib import Path
from typing import Any, Mapping

import bpy

from .bridge import get_client
from .catalog import HUNYUAN_LICENSE_URL, HUNYUAN_V2_LICENSE_URL, MODELS
from .properties import get_models_directory, get_preferences
from .ui_helpers import (
    ACTIVE_STATUSES,
    IN_PROGRESS_STATUSES,
    attempt_count,
    clamp_progress,
    failure_message,
    find_job,
    find_model_state,
    format_bytes,
    has_active_jobs,
    iter_transfers,
    normalize_status,
    read_field,
    resource_metric,
    result_path,
    saved_bytes_label,
    status_icon,
    status_label,
    storage_label,
    transfer_progress,
    validate_image_path,
)


_SNAPSHOT = {}
_RUNTIME_ERROR = ""
_ENGINE_NOTICE = ""
_DISCARD_ARMED: dict[str, float] = {}
_DISCARD_WINDOW_SECONDS = 20.0
# An invisible label still claims a line, so a notice row keeps its height whether
# or not a message fills it: nothing above the progress bar shifts when the engine
# starts reporting a stalled transfer.
NOTICE_SLOT = " "


def runtime_snapshot():
    return _SNAPSHOT


def engine_notice() -> str:
    """Last lifecycle answer the panel should show: deferred starts, pause reasons."""
    return _ENGINE_NOTICE


def set_engine_notice(message: str) -> None:
    """Record a lifecycle result for display; drawing itself stays read-only."""
    global _ENGINE_NOTICE
    _ENGINE_NOTICE = str(message or "")
    _tag_view3d_redraw()


def arm_discard(transfer_id: str) -> None:
    """Open the confirmation window for deleting this transfer's bytes."""
    _DISCARD_ARMED[transfer_id] = time.monotonic()


def discard_armed(transfer_id: str) -> bool:
    """True while a discard confirmation is still waiting for a second click."""
    opened = _DISCARD_ARMED.get(transfer_id)
    if opened is None:
        return False
    if time.monotonic() - opened > _DISCARD_WINDOW_SECONDS:
        _DISCARD_ARMED.pop(transfer_id, None)
        return False
    return True


def clear_discard(transfer_id: str) -> None:
    _DISCARD_ARMED.pop(transfer_id, None)


def _age_label(epoch: Any) -> str:
    """How long ago an engine timestamp was, in coarse units."""
    try:
        seconds = max(0, round(time.time() - float(epoch)))
    except (TypeError, ValueError):
        return ""
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"



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


def _model_id_containing(text):
    text = text.lower()
    for model_id, model in _catalog_models():
        if text in _model_name(model).lower() or text in model_id.lower():
            return model_id
    return ""


def _engine_failure():
    failure = _SNAPSHOT.get("failure") if isinstance(_SNAPSHOT, Mapping) else None
    return failure if isinstance(failure, Mapping) else None


def _draw_engine_status(layout):
    """Say whether the engine is alive, what it last answered, and how to start it."""
    alive = bool(read_field(_SNAPSHOT, "engine_alive", False))
    pid = read_field(_SNAPSHOT, "engine_pid", None)
    label = "Engine running" if alive else "Engine stopped"
    if pid:
        label = f"{label} (pid {pid})"
    row = layout.row()
    row.alert = not alive
    row.label(text=label, icon="CHECKMARK" if alive else "ERROR")
    heartbeat = read_field(_SNAPSHOT, "engine_heartbeat", None)
    if alive and heartbeat:
        age = _age_label(heartbeat)
        if age:
            layout.label(text=f"Heartbeat {age}", icon="TIME")
    notice = engine_notice()
    if notice:
        # One message, one row, one icon. A row stretches to its label, so the
        # text needs no hand-splitting; splitting it would print a second icon.
        notice_row = layout.row()
        notice_row.alert = not alive
        notice_row.label(text=notice, icon="INFO")
    lifecycle = layout.row(align=True)
    start = lifecycle.column()
    start.enabled = not alive
    start.operator("sculptr.initialize_engine", text="Start Engine", icon="PLAY")
    recover = lifecycle.column()
    recover.enabled = not alive
    recover.operator("sculptr.recover_engine", text="Recover", icon="FILE_REFRESH")
    layout.operator("sculptr.open_diagnostics", text="Open Diagnostics", icon="FILE_FOLDER")


def _draw_engine_failure(layout):
    """Surface the recorded engine death instead of leaving it inside a log."""
    failure = _engine_failure()
    if failure is None:
        return
    box = layout.box()
    box.alert = True
    message = str(read_field(failure, "message", "SculptR engine stopped"))
    code = str(read_field(failure, "error_code", read_field(failure, "reason", "")))
    box.label(text=failure_message(code, message) or message, icon="ERROR")
    reason = str(read_field(failure, "reason", ""))
    if reason and reason != code:
        box.label(text=f"Reason: {reason}", icon="INFO")
    exit_code = read_field(failure, "exit_code", None)
    if exit_code is not None:
        box.label(text=f"Exit code: {exit_code}", icon="INFO")
    recovered = str(read_field(failure, "recovered_at", ""))
    if recovered:
        box.label(text=f"Last recovery: {_age_label(recovered)}", icon="FILE_REFRESH")
    buttons = box.row(align=True)
    buttons.operator("sculptr.recover_engine", text="Recover Engine", icon="FILE_REFRESH")
    buttons.operator("sculptr.open_diagnostics", text="Open Diagnostics", icon="FILE_FOLDER")


def _draw_transfer_rows(layout):
    """One row per durable transfer record: the engine owns the bytes, this only reads.

    This section is observation-only on purpose. Resume, Pause, and Discard live on
    the model card in the Models section, so a transfer is never offered a second
    set of controls here and a redraw cannot restart, stop, or delete anything. The
    reason row is drawn every frame, empty slot included, so the bar and the counters
    below it stay put when a message arrives.
    """
    records = list(iter_transfers(_SNAPSHOT))
    if not records:
        return
    section = layout.box()
    section.label(text=f"Downloads ({len(records)})", icon="IMPORT")
    for record in records[-6:]:
        state = normalize_status(read_field(record, "state", ""))
        factor, saved, total = transfer_progress(record)
        retry = read_field(record, "retry", {})
        detail = str(read_field(record, "message", "") or "")
        error_code = str(read_field(record, "error_code", "") or "")
        card = section.box()
        title = card.row()
        title.label(text=str(read_field(record, "model_id", "Model")))
        if total:
            title.label(text=format_bytes(total))
        status_row = card.row()
        status_row.alert = state in {"failed", "cancelled", "discarded"}
        status_row.label(
            text=status_label(state, factor, detail, retry if isinstance(retry, Mapping) else None),
            icon=status_icon(state),
        )
        reason = failure_message(error_code, detail) if error_code else detail
        reason_row = card.row()
        if reason:
            reason_row.label(text=reason, icon="ERROR" if state == "failed" else "INFO")
        else:
            reason_row.label(text=NOTICE_SLOT)
        if factor > 0.0 or state in IN_PROGRESS_STATUSES or state == "paused":
            card.progress(
                factor=factor,
                text=f"{format_bytes(saved)} / {format_bytes(total)}" if total else format_bytes(saved),
            )
        saved_row = card.row()
        saved_row.label(text=saved_bytes_label(record), icon="FILE_FOLDER")
        saved_row.label(text=f"Attempt {attempt_count(record)}", icon="TIME")



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
        _draw_engine_failure(layout)


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
        _draw_engine_status(layout)
        _draw_transfer_rows(layout)

        buttons = layout.row(align=True)
        busy = has_active_jobs(_SNAPSHOT)
        change = buttons.column()
        change.enabled = not busy
        change.operator("sculptr.change_models_folder", text="Change Folder", icon="FILE_FOLDER")
        open_folder = buttons.column()
        open_folder.enabled = path.is_dir()
        open_folder.operator("sculptr.open_models_folder", text="Open in Explorer", icon="DISCLOSURE_TRI_RIGHT")
        if busy:
            hint = layout.row()
            hint.alert = True
            # One label, and short enough to fit this drawer: a label never widens a
            # panel, and Blender replaces the middle of an overflowing label with "...".
            hint.label(
                text="Download running. Pause or cancel it before changing the folder.",
                icon="INFO",
            )


class SCULPTR_PT_license(_SculptRChildPanel, bpy.types.Panel):
    bl_idname = "SCULPTR_PT_license"
    bl_label = "License"
    bl_order = 2

    @classmethod
    def poll(cls, context):
        return context.scene.sculptr.active_tab == "MODELS"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Review Hunyuan license", icon="INFO")
        layout.label(text="This extension is only a hub for you to download and launch the models.")
        layout.label(text="License for their use can only be granted by Tencent")
        layout.label(text=f"Hunyuan 3D 2.1: {HUNYUAN_LICENSE_URL}")
        layout.label(text=f"Hunyuan 3D 2.0: {HUNYUAN_V2_LICENSE_URL}")


class SCULPTR_PT_models(_SculptRChildPanel, bpy.types.Panel):
    bl_idname = "SCULPTR_PT_models"
    bl_label = "Models"
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return context.scene.sculptr.active_tab == "MODELS"

    def draw(self, context):
        layout = self.layout
        for model_id, model in _catalog_models():
            state = find_model_state(_SNAPSHOT, model_id)
            status = normalize_status(state.get("status"))
            progress = clamp_progress(state.get("progress", state.get("percent", 0.0)))
            detail = str(state.get("message", state.get("detail", state.get("error", ""))))
            job_id = str(state.get("job_id", state.get("id", "")))
            transfer_id = str(state.get("transfer_id", ""))
            error_code = str(state.get("error_code", ""))
            next_attempt = state.get("next_attempt_at")
            retry = {"next_attempt_at": next_attempt} if next_attempt else None
            saved = int(state.get("bytes_done") or 0)
            total = int(state.get("bytes_total") or 0)

            card = layout.box()
            title = card.row()
            title.label(text=_model_name(model))
            title.label(text=_model_size(model))
            status_row = card.row()
            status_row.alert = status in {"failed", "paused", "cancelled"}
            status_row.label(
                text=status_label(status, progress, detail, retry), icon=status_icon(status)
            )
            # The reason slot is drawn every frame, message or not: a row that only
            # appears when the engine reports a stalled transfer pushes the bar and
            # the buttons down, so the card reflows. It sits under the status line and
            # above the bar, so the message reads as the status it explains.
            reason = failure_message(error_code, detail) if error_code else ""
            reason_row = card.row()
            if reason:
                reason_row.label(text=reason, icon="ERROR")
            else:
                reason_row.label(text=NOTICE_SLOT)
            if status in IN_PROGRESS_STATUSES or status == "paused":
                card.progress(
                    factor=progress,
                    text=f"{format_bytes(saved)} / {format_bytes(total)}" if total else "",
                )

            actions = card.row(align=True)
            primary = actions.column()
            if status in IN_PROGRESS_STATUSES:
                primary.enabled = bool(job_id)
                operator = primary.operator("sculptr.cancel_job", text="Cancel", icon="CANCEL")
                operator.job_id = job_id
                pause = actions.column()
                pause.enabled = bool(transfer_id)
                operator = pause.operator("sculptr.pause_transfer", text="Pause", icon="PAUSE")
                operator.transfer_id = transfer_id
            elif status == "paused":
                primary.enabled = bool(transfer_id)
                operator = primary.operator("sculptr.resume_transfer", text="Resume", icon="PLAY")
                operator.transfer_id = transfer_id
            elif status in {"failed", "cancelled"}:
                operator = primary.operator("sculptr.model_action", text="Retry", icon="FILE_REFRESH")
                operator.action = "retry"
                operator.model_id = model_id
            elif status == "installed":
                primary.enabled = False
                primary.operator("sculptr.model_action", text="Installed", icon="CHECKMARK")
            else:
                primary.enabled = status not in ACTIVE_STATUSES
                operator = primary.operator("sculptr.model_action", text="Download", icon="IMPORT")
                operator.action = "download"
                operator.model_id = model_id

            check = actions.column()
            check.enabled = status not in ACTIVE_STATUSES
            operator = check.operator("sculptr.model_action", text="Check", icon="FILE_TICK")
            operator.action = "check"
            operator.model_id = model_id

            # Deleting a transfer belongs to this card and nowhere else: the Downloads
            # rows under the Folder tab only report what the engine is doing. The arm/
            # confirm pair keeps one stray click from throwing away committed bytes.
            if transfer_id and status not in {"installed", "discarded"}:
                if discard_armed(transfer_id):
                    confirm = actions.column()
                    confirm.alert = True
                    operator = confirm.operator(
                        "sculptr.discard_transfer", text="Confirm Discard", icon="TRASH"
                    )
                    operator.decision = "confirm"
                    operator.transfer_id = transfer_id
                    keep = actions.column()
                    operator = keep.operator(
                        "sculptr.discard_transfer", text="Keep", icon="CANCEL"
                    )
                    operator.decision = "keep"
                    operator.transfer_id = transfer_id
                else:
                    discard = actions.column()
                    operator = discard.operator(
                        "sculptr.discard_transfer", text="Discard", icon="TRASH"
                    )
                    operator.transfer_id = transfer_id


CLASSES = (
    SCULPTR_PT_root,
    SCULPTR_PT_image,
    SCULPTR_PT_resources,
    SCULPTR_PT_settings,
    SCULPTR_PT_enhance,
    SCULPTR_PT_folder,
    SCULPTR_PT_license,
    SCULPTR_PT_models,
)
