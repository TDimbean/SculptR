from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty


def _default_models_directory():
    return bpy.utils.user_resource("DATAFILES", path="sculptr/models")


class SculptRPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    models_directory: StringProperty(
        name="Models Folder",
        description="Folder containing SculptR models, runtimes, and job state",
        default=_default_models_directory(),
        subtype="DIR_PATH",
    )
    hunyuan_license_confirmed: BoolProperty(
        name="I accept the Hunyuan license for this US deployment",
        description=(
            "Review the applicable Tencent Hunyuan Community License; acceptance does not "
            "change or grant rights beyond that license"
        ),
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "models_directory")
        warning = layout.box()
        warning.alert = not self.hunyuan_license_confirmed
        warning.label(text="Review Hunyuan license before download", icon="ERROR")
        warning.prop(self, "hunyuan_license_confirmed")


class SculptRSettings(bpy.types.PropertyGroup):
    active_tab: EnumProperty(
        name="View",
        items=(
            ("GENERATE", "Generate", "Generate and enhance models"),
            ("MODELS", "Models", "Manage local model files"),
        ),
        default="GENERATE",
    )
    main_image: StringProperty(name="Main Image", subtype="FILE_PATH")
    left_image: StringProperty(name="Left View", subtype="FILE_PATH")
    right_image: StringProperty(name="Right View", subtype="FILE_PATH")
    back_image: StringProperty(name="Back View", subtype="FILE_PATH")
    last_image_slot: EnumProperty(
        name="Drop Target",
        items=(
            ("main", "Main", "Replace main image on next full drop set"),
            ("left", "Left", "Replace left image on next full drop set"),
            ("right", "Right", "Replace right image on next full drop set"),
            ("back", "Back", "Replace back image on next full drop set"),
        ),
        default="main",
    )
    quality: EnumProperty(
        name="Quality",
        items=(
            ("fast", "Fast", "Faster generation with fewer refinement steps"),
            ("high", "High", "Balanced quality and generation time"),
            ("ultra", "Ultra", "Maximum refinement and resource use"),
        ),
        default="high",
    )
    faces: IntProperty(
        name="Faces",
        description="Target mesh face count",
        default=100_000,
        min=10_000,
        max=1_000_000,
        soft_min=10_000,
        soft_max=1_000_000,
        step=1_000,
    )
    use_texture: BoolProperty(name="Texture", default=True)
    texture_size: EnumProperty(
        name="Texture Size",
        items=(
            ("1024", "1K", "1024 pixel textures"),
            ("2048", "2K", "2048 pixel textures"),
            ("4096", "4K", "4096 pixel textures"),
        ),
        default="2048",
    )
    seed: IntProperty(name="Seed", default=0, min=0, max=2_147_483_647)
    generation_job_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    enhance_job_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    utility_job_id: StringProperty(options={"HIDDEN", "SKIP_SAVE"})


def get_preferences(context):
    return context.preferences.addons[__package__].preferences


def get_models_directory(context) -> Path:
    return Path(bpy.path.abspath(get_preferences(context).models_directory)).expanduser().resolve()


def register_properties():
    bpy.types.Scene.sculptr = PointerProperty(type=SculptRSettings)


def unregister_properties():
    if hasattr(bpy.types.Scene, "sculptr"):
        del bpy.types.Scene.sculptr


CLASSES = (SculptRPreferences, SculptRSettings)
