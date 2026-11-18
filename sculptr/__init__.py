def _bootstrap_engine():
    """One-shot engine start, deferred off the draw loop; never raises."""
    from .operators import bootstrap_engine

    bootstrap_engine()
    return None


def _start_engine_bootstrap():
    import bpy

    if not bpy.app.timers.is_registered(_bootstrap_engine):
        # Deferred on purpose: enabling the add-on must not spawn a process or
        # block the interface, and a failed start is reported in the panel.
        bpy.app.timers.register(_bootstrap_engine, first_interval=1.0)


def _stop_engine_bootstrap():
    import bpy

    if bpy.app.timers.is_registered(_bootstrap_engine):
        bpy.app.timers.unregister(_bootstrap_engine)


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
    _start_engine_bootstrap()


def unregister():
    import bpy

    from .operators import CLASSES as OPERATOR_CLASSES
    from .properties import CLASSES as PROPERTY_CLASSES
    from .properties import unregister_properties
    from .ui import CLASSES as UI_CLASSES
    from .ui import stop_runtime_poll

    _stop_engine_bootstrap()
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
