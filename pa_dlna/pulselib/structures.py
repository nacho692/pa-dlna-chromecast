"""Ctypes structures."""

import sysconfig
import ctypes as ct

def _time_t():
    """Return a ctypes type for time_t."""

    sizeof_time_t = sysconfig.get_config_var('SIZEOF_TIME_T')
    types = (ct.c_longlong, ct.c_long, ct.c_int)
    sizes = [ct.sizeof(t) for t in types]
    assert sizeof_time_t in sizes, 'Cannot find a ctypes match for time_t.'
    return types[sizes.index(sizeof_time_t)]

class TIMEVAL(ct.Structure):
    _fields_ = [
        ('tv_sec', _time_t()),
        ('tv_usec', ct.c_long),
    ]

class PA_SAMPLE_SPEC(ct.Structure):
    _fields_ = [
        ('sample_format', ct.c_int),        # pa_sample_format_t format;
        ('rate', ct.c_uint32),              # uint32_t rate;
        ('channels', ct.c_uint8),           # uint8_t channels;
    ]

class PA_CHANNEL_MAP(ct.Structure):
    _fields_ = [
        ('channels', ct.c_uint8),           # uint8_t channels;
        ('channel_position', ct.c_int * 32),# pa_channel_position_t map[32U];
    ]

class PA_CVOLUME(ct.Structure):
    _fields_ = [
        ('channels', ct.c_uint8),           # uint8_t channels;
        ('vomume', ct.c_uint32 * 32),       # pa_volume_t values[32U];
    ]
class PA_SINK_INFO(ct.Structure):
    _fields_ = [
        ('name', ct.c_char_p),              # const char *name;
        ('index', ct.c_uint32),             # uint32_t index;
        ('description', ct.c_char_p),       # const char *description;
        ('sample_spec', PA_SAMPLE_SPEC),    # pa_sample_spec sample_spec;
        ('channel_map', PA_CHANNEL_MAP),    # pa_channel_map channel_map;
        ('owner_module', ct.c_uint32),      # uint32_t owner_module;
        ('volume', PA_CVOLUME),             # pa_cvolume volume;
        ('mute', ct.c_int),                 # int mute;
        ('monitor_source', ct.c_uint32),    # uint32_t monitor_source;
        ('monitor_source_name', ct.c_char_p),# const char *monitor_source_name
        ('latency', ct.c_uint64),           # pa_usec_t latency;
        ('driver', ct.c_char_p),            # const char *driver;
        ('flags', ct.c_int),                # pa_sink_flags_t flags;
        ('proplist', ct.c_void_p),          # pa_proplist *proplist;
        ('configured_latency', ct.c_uint64),# pa_usec_t configured_latency;
        ('base_volume', ct.c_uint32),       # pa_volume_t base_volume;
        ('state', ct.c_int),                # pa_sink_state_t state;
        ('n_volume_steps', ct.c_uint32),    # uint32_t n_volume_steps;
        ('card', ct.c_uint32),              # uint32_t card;
        ('n_ports', ct.c_uint32),           # uint32_t n_ports;
        ('ports', ct.c_void_p),             # pa_sink_port_info** ports;
        ('active_port', ct.c_void_p),       # pa_sink_port_info* active_port;
        ('n_formats', ct.c_uint32),         # uint8_t n_formats;
        ('formats', ct.c_void_p),           # pa_format_info **formats;
    ]

class PA_SINK_INPUT_INFO(ct.Structure):
    _fields_ = [
        ('index', ct.c_uint32),             # uint32_t index;
        ('name', ct.c_char_p),              # const char *name;
        ('owner_module', ct.c_uint32),      # uint32_t owner_module;
        ('client', ct.c_uint32),            # uint32_t client;
        ('sink', ct.c_uint32),              # uint32_t sink;
        ('sample_spec', PA_SAMPLE_SPEC),    # pa_sample_spec sample_spec;
        ('channel_map', PA_CHANNEL_MAP),    # pa_channel_map channel_map;
        ('volume', PA_CVOLUME),             # pa_cvolume volume;
        ('buffer_usec', ct.c_uint64),       # pa_usec_t buffer_usec;
        ('sink_usec', ct.c_uint64),         # pa_usec_t sink_usec;
        ('resample_method', ct.c_char_p),   # const char *resample_method;
        ('driver', ct.c_char_p),            # const char *driver;
        ('mute', ct.c_int),                 # int mute;
        ('proplist', ct.c_void_p),          # pa_proplist *proplist;
        ('corked', ct.c_int),               # int corked;
        ('has_volume', ct.c_int),           # int has_volume;
        ('volume_writable', ct.c_int),      # int volume_writable;
        ('format', ct.c_void_p),            # pa_format_info *format;
    ]

class PA_SERVER_INFO(ct.Structure):
    _fields_ = [
        ('user_name', ct.c_char_p),         # User name of the daemon process
        ('host_name', ct.c_char_p),         # Host name the daemon is running on
        ('server_version', ct.c_char_p),    # Version string of the daemon
        ('server_name', ct.c_char_p),       # Server package name
        ('sample_spec', PA_SAMPLE_SPEC),    # Default sample specification
        ('default_sink_name', ct.c_char_p), # Name of default sink.
        ('default_source_name', ct.c_char_p),# Name of default source.
        ('cookie', ct.c_uint32),            # A random cookie for identifying
                                            # this instance of PulseAudio.
        ('channel_map', PA_CHANNEL_MAP),    # Default channel map.
    ]
