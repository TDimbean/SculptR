from pathlib import Path
import uuid
from typing import Any, Mapping

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from .bridge import get_client
from .bridge import initialize as initialize_engine
from .bridge import settings_from_context
from .catalog import get_model
from .properties import get_models_directory, get_preferences
from .ui import arm_discard, clear_discard, discard_armed, set_engine_notice
from .ui_helpers import (
    IN_PROGRESS_STATUSES,
    choose_drop_slot,
    find_model_state,
    find_transfer,
    format_bytes,
    normalize_status,
    read_field,
    saved_bytes_label,
    transfer_busy,
    transfer_for_model,
    transfer_progress,
    validate_image_path,
)


IMAGE_SLOT_ITEMS = (
    ("main", "Main", "Main image"),
    ("left", "Left", "Optional left view"),
    ("right", "Right", "Optional right view"),
    ("back", "Back", "Optional back view"),
)


def _slot_property(slot):
    return "main_image" if slot == "main" else f"{slot}_image"


def _submit(context, action, payload):
    return str(get_client(context).submit(action, payload))


def _settings_payload(settings):
    return {
        "quality": settings.quality,
        "faces": settings.faces,
        "texture": settings.use_texture,
        "texture_size": int(settings.texture_size),
        "seed": settings.seed,
    }


def _image_inputs(settings, mesh=None):
    inputs = {
        "front": settings.main_image or None,
        "left": settings.left_image or None,
        "right": settings.right_image or None,
        "back": settings.back_image or None,
    }
    if mesh is not None:
        inputs["mesh"] = str(mesh)
    return inputs


def _output_directory(context):
    return get_models_directory(context) / ".sculptr" / "outputs" / uuid.uuid4().hex


def _bridge_error_message(error: BaseException) -> str:
    return str(error).strip() or error.__class__.__name__


def _report_bridge_error(operator, error: BaseException, prefix: str = "") -> str:
    """Turn a bridge refusal into panel text and a report, never a traceback."""
    message = _bridge_error_message(error)
    if prefix:
        message = f"{prefix}: {message}"
    set_engine_notice(message)
    operator.report({"ERROR"}, message)
    return message


_RECOVERY_NOTICES = {
    "engine_not_confirmed_dead": (
        "Recovery deferred: the previous engine is still alive or its state cannot be "
        "verified, so no second engine was started and nothing was killed."
    ),
    "restart_backoff": (
        "Recovery deferred: the engine died repeatedly, so the restart waits "
        "{retry_in_seconds}s instead of crash-looping. Saved bytes are untouched."
    ),
    "start_failed": "The engine could not start: {message}",
}


def _recovery_notice(result: Mapping[str, Any]) -> str:
    """User-facing explanation of a `recover()` result, including why it waited."""
    if read_field(result, "started"):
        pid = read_field(result, "engine_pid", "")
        return f"Engine running (pid {pid})"
    reason = str(read_field(result, "reason", "unknown"))
    template = _RECOVERY_NOTICES.get(reason, "Recovery deferred ({reason}).")
    return template.format(
        retry_in_seconds=read_field(result, "retry_in_seconds", "a few seconds"),
        message=read_field(result, "message", "unknown error"),
    )


def _safe_recover(client, *, reason: str) -> dict[str, Any]:
    """`recover` raises when the root is unknown; report instead of escaping."""
    try:
        return dict(client.recover(reason=reason))
    except Exception as error:
        return {"started": False, "reason": "start_failed", "message": _bridge_error_message(error)}


def _transfer_context(context, required: str = "") -> tuple[Any, dict[str, Any]]:
    """(client, transfer record) for a panel-supplied transfer id; record may be empty."""
    client = get_client(context)
    if not required:
        return client, {}
    try:
        record = find_transfer(client.snapshot() or {}, required)
    except Exception:
        record = {}
    return client, record


def _model_is_downloading(context, model_id: str) -> bool:
    """True when this model already has a live job or a resumable transfer."""
    try:
        snapshot = get_client(context).snapshot() or {}
    except Exception:
        return False
    if normalize_status(find_model_state(snapshot, model_id).get("status")) in IN_PROGRESS_STATUSES:
        return True
    return transfer_busy(transfer_for_model(snapshot, model_id))


def bootstrap_engine() -> None:
    """Bind and start the engine once at enable time, off the draw loop.

    Never raises into Blender and never blocks: an unset models folder or an
    engine that cannot boot becomes panel text. The journals stay readable
    either way, so the panel still lists saved bytes and offers Start Engine.
    """
    try:
        if settings_from_context(bpy.context) is None:
            set_engine_notice("Set the models folder in the Folder panel, then press Start Engine.")
            return
        client = initialize_engine(bpy.context)
        snapshot = client.snapshot() or {}
    except Exception as error:
        set_engine_notice(f"The engine is not running yet: {_bridge_error_message(error)}")
        return
    if read_field(snapshot, "engine_alive"):
        set_engine_notice(f"Engine running (pid {read_field(snapshot, 'engine_pid', '')})")
        return
    set_engine_notice(_recovery_notice(_safe_recover(client, reason="initialize")))


class SCULPTR_OT_choose_image(bpy.types.Operator):
    bl_idname = "sculptr.choose_image"
    bl_label = "Choose Image"
    bl_description = "Choose an input image"

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.webp", options={"HIDDEN"})
    slot: EnumProperty(items=IMAGE_SLOT_ITEMS, default="main", options={"SKIP_SAVE"})

    def invoke(self, context, event):
        context.scene.sculptr.last_image_slot = self.slot
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        valid, message = validate_image_path(self.filepath)
        if not valid:
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        settings = context.scene.sculptr
        setattr(settings, _slot_property(self.slot), str(Path(self.filepath).resolve()))
        settings.last_image_slot = self.slot
        return {"FINISHED"}


class SCULPTR_OT_clear_image(bpy.types.Operator):
    bl_idname = "sculptr.clear_image"
    bl_label = "Clear Image"
    bl_description = "Remove this image from the generation inputs"

    slot: EnumProperty(items=IMAGE_SLOT_ITEMS, default="main", options={"SKIP_SAVE"})

    def execute(self, context):
        setattr(context.scene.sculptr, _slot_property(self.slot), "")
        return {"FINISHED"}


class SCULPTR_OT_drop_image(bpy.types.Operator):
    bl_idname = "sculptr.drop_image"
    bl_label = "Use Image in SculptR"
    bl_description = "Fill the first empty SculptR image slot"

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})

    def execute(self, context):
        valid, message = validate_image_path(self.filepath)
        if not valid:
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        settings = context.scene.sculptr
        paths = {
            "main": settings.main_image,
            "left": settings.left_image,
            "right": settings.right_image,
            "back": settings.back_image,
        }
        slot = choose_drop_slot(paths, settings.last_image_slot)
        setattr(settings, _slot_property(slot), str(Path(self.filepath).resolve()))
        settings.last_image_slot = slot
        self.report({"INFO"}, f"Image assigned to {slot.title()}")
        return {"FINISHED"}


class SCULPTR_FH_image_drop(bpy.types.FileHandler):
    bl_idname = "SCULPTR_FH_image_drop"
    bl_label = "SculptR image drop"
    bl_import_operator = "sculptr.drop_image"
    bl_file_extensions = ".png;.jpg;.jpeg;.webp"

    @classmethod
    def poll_drop(cls, context):
        return bool(context.area and context.area.type == "VIEW_3D")


class SCULPTR_OT_change_models_folder(bpy.types.Operator):
    bl_idname = "sculptr.change_models_folder"
    bl_label = "Change Models Folder"
    bl_description = "Choose where SculptR stores models and runtime state"

    directory: StringProperty(subtype="DIR_PATH", options={"SKIP_SAVE"})

    def invoke(self, context, event):
        self.directory = str(get_models_directory(context))
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        directory = Path(self.directory).expanduser().resolve()
        if not directory.is_dir():
            self.report({"ERROR"}, "Choose an existing folder")
            return {"CANCELLED"}
        client = get_client(context)
        # `has_busy_work` counts resumable transfers, not only running jobs:
        # retiring the engine while paused bytes exist is what makes a download
        # restart from byte zero.
        try:
            busy = client.has_busy_work()
        except Exception as error:
            _report_bridge_error(self, error, prefix="Cannot check running downloads")
            return {"CANCELLED"}
        if busy:
            self.report(
                {"ERROR"},
                "A download is still running. Pause or cancel it before changing the models folder.",
            )
            return {"CANCELLED"}
        get_preferences(context).models_directory = str(directory)
        try:
            # The engine follows the new root now: `configure` retires the old
            # session and starts a client bound to the folder just chosen.
            client.configure(directory)
        except Exception as error:
            # The folder is already the user's; the engine can be started later
            # with Start Engine, which reads this same preference.
            message = _report_bridge_error(
                self, error, prefix="Models folder set, but the engine did not start"
            )
            self.report({"WARNING"}, message)
            return {"FINISHED"}
        set_engine_notice("")
        self.report({"INFO"}, f"Models folder set to {directory}; the engine follows it")
        return {"FINISHED"}


class SCULPTR_OT_open_models_folder(bpy.types.Operator):
    bl_idname = "sculptr.open_models_folder"
    bl_label = "Open in Explorer"
    bl_description = "Open the models folder in the system file browser"

    def execute(self, context):
        directory = get_models_directory(context)
        if not directory.is_dir():
            self.report({"ERROR"}, "Models folder does not exist")
            return {"CANCELLED"}
        return bpy.ops.wm.path_open(filepath=str(directory))


class SCULPTR_OT_open_diagnostics(bpy.types.Operator):
    bl_idname = "sculptr.open_diagnostics"
    bl_label = "Open Diagnostics"
    bl_description = "Open the folder with engine logs, bridge logs, and archived job records"

    def execute(self, context):
        try:
            snapshot = get_client(context).snapshot() or {}
            logs = Path(str(read_field(snapshot, "log_path", ""))).parent
            archive = Path(str(read_field(snapshot, "jobs_path", "")))
        except Exception as error:
            _report_bridge_error(self, error, prefix="Diagnostics are unavailable")
            return {"CANCELLED"}
        target = logs if logs.is_dir() else archive
        if not target.is_dir():
            self.report({"ERROR"}, "Diagnostics have not been written yet")
            return {"CANCELLED"}
        return bpy.ops.wm.path_open(filepath=str(target))


class SCULPTR_OT_model_action(bpy.types.Operator):
    bl_idname = "sculptr.model_action"
    bl_label = "Model Action"

    action: EnumProperty(
        items=(
            ("download", "Download", "Download this model"),
            ("retry", "Retry", "Retry this model download"),
            ("check", "Check", "Check local model files"),
        ),
        options={"SKIP_SAVE"},
    )
    model_id: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        try:
            get_model(self.model_id)
            if self.action in {"download", "retry"}:
                if not getattr(bpy.app, "online_access", True):
                    self.report({"ERROR"}, "Enable Online Access in Blender Preferences")
                    return {"CANCELLED"}
                # A second click must not queue a second job for the same model:
                # the first one already owns the saved bytes.
                if _model_is_downloading(context, self.model_id):
                    self.report(
                        {"INFO"},
                        "This model is already downloading; saved bytes resume. Nothing new was queued.",
                    )
                    return {"FINISHED"}
            action = "check_model" if self.action == "check" else "download_model"
            _submit(
                context,
                action,
                {
                    "model_id": self.model_id,
                    "models_directory": str(get_models_directory(context)),
                },
            )
        except Exception as error:
            _report_bridge_error(self, error, prefix="Could not queue this request")
            return {"CANCELLED"}
        return {"FINISHED"}


class SCULPTR_OT_cancel_job(bpy.types.Operator):
    bl_idname = "sculptr.cancel_job"
    bl_label = "Cancel"
    bl_description = "Cancel this job while preserving resumable downloads"

    job_id: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        if not self.job_id:
            self.report({"ERROR"}, "Job is not available yet")
            return {"CANCELLED"}
        try:
            get_client(context).cancel(self.job_id)
        except Exception as error:
            _report_bridge_error(self, error, prefix="Cancel failed")
            return {"CANCELLED"}
        return {"FINISHED"}


class SCULPTR_OT_initialize_engine(bpy.types.Operator):
    bl_idname = "sculptr.initialize_engine"
    bl_label = "Start Engine"
    bl_description = (
        "Bind the download engine to the models folder and start it, or recover it "
        "if it died. Saved bytes resume from where they stopped"
    )

    def execute(self, context):
        try:
            client = initialize_engine(context)
        except Exception as error:
            _report_bridge_error(self, error, prefix="The engine cannot start")
            return {"CANCELLED"}
        if read_field(client.snapshot() or {}, "engine_alive"):
            notice = f"Engine running (pid {read_field(client.snapshot() or {}, 'engine_pid', '')})"
            set_engine_notice(notice)
            self.report({"INFO"}, notice)
            return {"FINISHED"}
        notice = _recovery_notice(_safe_recover(client, reason="initialize"))
        set_engine_notice(notice)
        if notice.startswith("Engine running"):
            self.report({"INFO"}, notice)
            return {"FINISHED"}
        self.report({"WARNING"}, notice)
        return {"CANCELLED"}


class SCULPTR_OT_recover_engine(bpy.types.Operator):
    bl_idname = "sculptr.recover_engine"
    bl_label = "Recover Engine"
    bl_description = (
        "Restart the engine after a crash, reboot, or lost network. Interrupted "
        "downloads continue from their verified saved bytes"
    )

    def execute(self, context):
        try:
            client = get_client(context)
        except Exception as error:
            _report_bridge_error(self, error, prefix="Recovery is unavailable")
            return {"CANCELLED"}
        notice = _recovery_notice(_safe_recover(client, reason="user_recovery"))
        set_engine_notice(notice)
        if notice.startswith("Engine running"):
            self.report({"INFO"}, notice)
            return {"FINISHED"}
        self.report({"WARNING"}, notice)
        return {"CANCELLED"}


class SCULPTR_OT_pause_transfer(bpy.types.Operator):
    bl_idname = "sculptr.pause_transfer"
    bl_label = "Pause"
    bl_description = (
        "Stop writing now and keep every committed byte; Resume continues from them"
    )

    transfer_id: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        if not self.transfer_id:
            self.report({"ERROR"}, "This transfer is not available yet")
            return {"CANCELLED"}
        client, record = _transfer_context(context, self.transfer_id)
        try:
            client.pause_transfer(self.transfer_id)
        except Exception as error:
            _report_bridge_error(self, error, prefix="Pause failed")
            return {"CANCELLED"}
        model = str(read_field(record, "model_id", self.transfer_id))
        notice = f"Paused {model}. {saved_bytes_label(record)} stay on disk."
        set_engine_notice(notice)
        self.report({"INFO"}, notice)
        return {"FINISHED"}


class SCULPTR_OT_resume_transfer(bpy.types.Operator):
    bl_idname = "sculptr.resume_transfer"
    bl_label = "Resume"
    bl_description = (
        "Continue this download from its saved bytes; it restarts from zero only "
        "when the server no longer supports ranges or the bytes fail verification"
    )

    transfer_id: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        if not self.transfer_id:
            self.report({"ERROR"}, "This transfer is not available yet")
            return {"CANCELLED"}
        client, record = _transfer_context(context, self.transfer_id)
        authorization = read_field(record, "authorization", {})
        # An intent written before this build, or one the engine left paused on the
        # authorization gate, carries license_authorized false. The hub grants no
        # acceptance step, so Resume states the authorization itself; otherwise
        # Resume would re-pause on the same gate and the bytes would never move.
        if (
            isinstance(authorization, Mapping)
            and read_field(authorization, "license_required")
            and not read_field(authorization, "license_authorized")
        ):
            try:
                client.authorize_transfer(self.transfer_id, True)
            except Exception as error:
                self.report(
                    {"WARNING"},
                    f"Could not record the authorization: {_bridge_error_message(error)}",
                )
        try:
            client.resume_transfer(self.transfer_id)
        except Exception as error:
            _report_bridge_error(self, error, prefix="Resume failed")
            return {"CANCELLED"}
        model = str(read_field(record, "model_id", self.transfer_id))
        notice = f"Resuming {model}: {saved_bytes_label(record)}. "
        notice += (
            "It continues from the saved offset unless the server rejects the range."
        )
        set_engine_notice(notice)
        self.report({"INFO"}, notice)
        return {"FINISHED"}


class SCULPTR_OT_discard_transfer(bpy.types.Operator):
    bl_idname = "sculptr.discard_transfer"
    bl_label = "Discard Transfer"
    bl_description = (
        "Delete this transfer and its downloaded bytes. Needs a second confirmation"
    )

    transfer_id: StringProperty(options={"SKIP_SAVE"})
    decision: EnumProperty(
        items=(
            ("request", "Discard", "Ask for confirmation before deleting"),
            ("confirm", "Confirm Discard", "Delete now"),
            ("keep", "Keep", "Cancel the pending discard"),
        ),
        default="request",
        options={"SKIP_SAVE"},
    )
    confirmed: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        # Explicit invocation already comes from a menu or dialog: confirm once.
        self.confirmed = True
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        if not self.transfer_id:
            self.report({"ERROR"}, "This transfer is not available yet")
            return {"CANCELLED"}
        client, record = _transfer_context(context, self.transfer_id)
        model = str(read_field(record, "model_id", self.transfer_id))
        deleted = format_bytes(transfer_progress(record)[1]) if record else "its downloaded bytes"
        if self.decision == "keep":
            clear_discard(self.transfer_id)
            self.report({"INFO"}, f"Kept {model}: {deleted} left untouched")
            return {"FINISHED"}
        if not self.confirmed and not discard_armed(self.transfer_id):
            arm_discard(self.transfer_id)
            self.report(
                {"WARNING"},
                f"Discard deletes {deleted} of {model}. Press Discard again to confirm.",
            )
            return {"CANCELLED"}
        clear_discard(self.transfer_id)
        try:
            client.discard_transfer(self.transfer_id)
        except Exception as error:
            _report_bridge_error(self, error, prefix="Discard failed")
            return {"CANCELLED"}
        notice = f"Discarded {model}: {deleted} deleted. A new download starts from zero."
        set_engine_notice(notice)
        self.report({"INFO"}, notice)
        return {"FINISHED"}


class SCULPTR_OT_generate(bpy.types.Operator):
    bl_idname = "sculptr.generate"
    bl_label = "Generate Model"
    bl_description = "Generate a 3D model in the background engine"

    def execute(self, context):
        settings = context.scene.sculptr
        valid, message = validate_image_path(settings.main_image)
        if not valid:
            self.report({"ERROR"}, f"Main image: {message}")
            return {"CANCELLED"}
        inputs = _image_inputs(settings)
        for name, path in inputs.items():
            if path:
                valid, message = validate_image_path(path)
                if not valid:
                    self.report({"ERROR"}, f"{name.title()} image: {message}")
                    return {"CANCELLED"}
        try:
            settings.generation_job_id = _submit(
                context,
                "generate",
                {
                    "models_directory": str(get_models_directory(context)),
                    "output_directory": str(_output_directory(context)),
                    "inputs": inputs,
                    "settings": _settings_payload(settings),
                },
            )
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class SCULPTR_OT_clear_cache(bpy.types.Operator):
    bl_idname = "sculptr.clear_cache"
    bl_label = "Clear Cache"
    bl_description = "Clear temporary engine caches without deleting installed models"

    def execute(self, context):
        try:
            context.scene.sculptr.utility_job_id = _submit(
                context,
                "clear_cache",
                {"models_directory": str(get_models_directory(context))},
            )
        except Exception as error:
            self.report({"ERROR"}, _bridge_error_message(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class SCULPTR_OT_enhance(bpy.types.Operator):
    bl_idname = "sculptr.enhance"
    bl_label = "Enhance"

    action: EnumProperty(
        items=(
            ("repaint_texture", "Repaint Texture", "Repaint the selected mesh"),
            ("enhance_detail", "Enhance Detail", "Generate replacement detailed geometry"),
        ),
        options={"SKIP_SAVE"},
    )

    def execute(self, context):
        active = context.active_object
        if active is None or active.type != "MESH":
            self.report({"ERROR"}, "Select a mesh object")
            return {"CANCELLED"}
        settings = context.scene.sculptr
        valid, message = validate_image_path(settings.main_image)
        if not valid:
            self.report({"ERROR"}, f"Main image: {message}")
            return {"CANCELLED"}
        for slot in ("left", "right", "back"):
            path = getattr(settings, f"{slot}_image")
            if path and not validate_image_path(path)[0]:
                self.report({"ERROR"}, f"{slot.title()} image is invalid")
                return {"CANCELLED"}
        input_directory = get_models_directory(context) / ".sculptr" / "inputs"
        input_directory.mkdir(parents=True, exist_ok=True)
        input_path = input_directory / f"{uuid.uuid4().hex}.glb"
        try:
            result = bpy.ops.export_scene.gltf(
                filepath=str(input_path),
                export_format="GLB",
                use_selection=True,
            )
            if "FINISHED" not in result:
                raise RuntimeError("Could not export selected mesh")
            settings.enhance_job_id = _submit(
                context,
                self.action,
                {
                    "models_directory": str(get_models_directory(context)),
                    "output_directory": str(_output_directory(context)),
                    "inputs": _image_inputs(settings, input_path),
                    "settings": _settings_payload(settings),
                    "object_name": active.name,
                },
            )
        except Exception as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class SCULPTR_OT_import_result(bpy.types.Operator):
    bl_idname = "sculptr.import_result"
    bl_label = "Import Result"
    bl_description = "Import the completed GLB into this scene"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})

    def execute(self, context):
        path = Path(self.filepath).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in {".glb", ".gltf"}:
            self.report({"ERROR"}, "Completed GLB result was not found")
            return {"CANCELLED"}
        return bpy.ops.import_scene.gltf(filepath=str(path))


class SCULPTR_OT_undo(bpy.types.Operator):
    bl_idname = "sculptr.undo"
    bl_label = "Undo"
    bl_description = "Undo the last Blender operation"

    def execute(self, context):
        return bpy.ops.ed.undo()


CLASSES = (
    SCULPTR_OT_choose_image,
    SCULPTR_OT_clear_image,
    SCULPTR_OT_drop_image,
    SCULPTR_FH_image_drop,
    SCULPTR_OT_change_models_folder,
    SCULPTR_OT_open_models_folder,
    SCULPTR_OT_open_diagnostics,
    SCULPTR_OT_model_action,
    SCULPTR_OT_cancel_job,
    SCULPTR_OT_initialize_engine,
    SCULPTR_OT_recover_engine,
    SCULPTR_OT_pause_transfer,
    SCULPTR_OT_resume_transfer,
    SCULPTR_OT_discard_transfer,
    SCULPTR_OT_generate,
    SCULPTR_OT_clear_cache,
    SCULPTR_OT_enhance,
    SCULPTR_OT_import_result,
    SCULPTR_OT_undo,
)
