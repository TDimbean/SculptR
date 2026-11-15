def register():
    import bpy

    from .operators import CLASSES as OPERATOR_CLASSES
    from .properties import CLASSES as PROPERTY_CLASSES
    from .properties import register_properties
    from .ui import CLASSES as UI_CLASSES
    from .ui import start_runtime_poll

    for cls in (*PROPERTY_CLASSES, *OPERATOR_CLASSES, *UI_CLASSES):
        bpy.utils.register_class(cls)
    register_properties()
    start_runtime_poll()


def unregister():
    import bpy

    from .operators import CLASSES as OPERATOR_CLASSES
    from .properties import CLASSES as PROPERTY_CLASSES
    from .properties import unregister_properties
    from .ui import CLASSES as UI_CLASSES
    from .ui import stop_runtime_poll

    stop_runtime_poll()
    try:
        # `stop` must not re-configure: a disabled add-on may never start a new
        # engine, it only has to shut the current one down cleanly.
        from .bridge import stop as stop_bridge

        stop_bridge()
    except Exception:
        pass
    unregister_properties()
    for cls in reversed((*PROPERTY_CLASSES, *OPERATOR_CLASSES, *UI_CLASSES)):
        bpy.utils.unregister_class(cls)
