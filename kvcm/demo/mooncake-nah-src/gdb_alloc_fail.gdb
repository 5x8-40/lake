# Attach: gdb -p $(pgrep -n -f '[m]ooncake_master --rpc') -x thisfile
# Logs to /tmp/nah_alloc_fail.log
set pagination off
set confirm off
set print thread-events off
set logging file /tmp/nah_alloc_fail.log
set logging overwrite on
set logging enabled on

# Resolve symbols in libmooncake_store.so
sharedlibrary libmooncake_store.so

# Break on allocate entry; on return null, call getLargestFreeRegion/size/capacity
break mooncake::OffsetBufferAllocator::allocate(unsigned long)
commands
  silent
  set $self = (void *)$x0
  set $req = (unsigned long)$x1
  finish
  # aarch64: unique_ptr typically returned in x0 (pointer payload)
  if $x0 == 0
    set $cap = (unsigned long)((size_t (*)(void *))mooncake::OffsetBufferAllocator::capacity() const)($self)
    set $used = (unsigned long)((size_t (*)(void *))mooncake::OffsetBufferAllocator::size() const)($self)
    set $lfree = (unsigned long)((size_t (*)(void *))mooncake::OffsetBufferAllocator::getLargestFreeRegion() const)($self)
    printf "NAH_ALLOC_FAIL request=%lu used=%lu capacity=%lu sum_free=%ld largest_free=%lu frag=%d\n", \
      $req, $used, $cap, ($cap>$used?$cap-$used:0), $lfree, \
      ($lfree < $req && ($cap-$used) >= $req)
  end
  continue
end

printf "gdb nah probe armed\n"
continue
