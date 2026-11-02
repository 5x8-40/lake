#!/usr/bin/env python3
"""Attach to mooncake_master; on OffsetBufferAllocator::allocate returning null,
print request / used / capacity / largest_free via calling into the live .so.

Usage (inside lake-test container):
  python3 -m pip install gdb  # not needed — use system gdb with this as -x via gdb's python
  Or:
  gdb -p $(pgrep -n mooncake_master) -x gdb_alloc_fail.gdb
"""
