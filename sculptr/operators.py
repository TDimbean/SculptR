from pathlib import Path
import uuid

import bpy
from bpy.props import EnumProperty, StringProperty

from .bridge import get_client
from .catalog import get_model
from .properties import get_models_directory, get_preferences
from .ui_helpers import (
    choose_drop_slot,
    has_active_jobs,
    read_field,
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


def _requires_hunyuan_license(model_id, model):
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
        if has_active_jobs(client.snapshot()):
            self.report({"ERROR"}, "Cancel active jobs before changing the models folder")
            return {"CANCELLED"}
        client.stop()
        get_preferences(context).models_directory = str(directory)
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
            model = get_model(self.model_id)
            preferences = get_preferences(context)
            if self.action in {"download", "retry"}:
                if _requires_hunyuan_license(self.model_id, model) and not preferences.hunyuan_license_confirmed:
                    self.report({"ERROR"}, "Accept the applicable Hunyuan license before downloading")
                    return {"CANCELLED"}
                if not getattr(bpy.app, "online_access", True):
                    self.report({"ERROR"}, "Enable Online Access in Blender Preferences")
                    return {"CANCELLED"}
            action = "check_model" if self.action == "check" else "download_model"
            _submit(
                context,
                action,
                {
                    "model_id": self.model_id,
                    "models_directory": str(get_models_directory(context)),
                    "license_confirmed": bool(preferences.hunyuan_license_confirmed),
                },
            )
        except Exception as error:
            self.report({"ERROR"}, str(error))
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
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
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
            self.report({"ERROR"}, str(error))
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
    SCULPTR_OT_model_action,
    SCULPTR_OT_cancel_job,
    SCULPTR_OT_generate,
    SCULPTR_OT_clear_cache,
    SCULPTR_OT_enhance,
    SCULPTR_OT_import_result,
    SCULPTR_OT_undo,
)
