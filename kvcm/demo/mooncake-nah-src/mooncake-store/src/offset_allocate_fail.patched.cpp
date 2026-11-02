// Drop-in replacement for OffsetBufferAllocator::allocate failure block
// in mooncake-store/src/allocator.cpp (v0.3.11.post1)
        auto allocation_handle = offset_allocator_->allocate(size);
        if (!allocation_handle) {
            size_t largest_free = 0;
            try {
                largest_free = offset_allocator_->storageReport().largestFreeRegion;
            } catch (...) {
            }
            const size_t used = cur_size_.load();
            LOG(ERROR) << "action=offset_alloc_fail"
                       << ", segment=" << segment_name_
                       << ", request=" << size
                       << ", used=" << used
                       << ", capacity=" << total_size_
                       << ", sum_free=" << (total_size_ > used ? total_size_ - used : 0)
                       << ", largest_free=" << largest_free
                       << ", frag=" << (largest_free < size &&
                                       (total_size_ > used ? total_size_ - used : 0) >= size);
            return nullptr;
        }
