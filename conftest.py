import resource

# Increase the maximum number of open file descriptors to avoid
# "Too many open files" during pytest cleanup.
try:
    resource.setrlimit(resource.RLIMIT_NOFILE, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
except Exception:
    pass
