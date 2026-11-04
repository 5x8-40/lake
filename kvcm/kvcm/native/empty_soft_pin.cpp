// 把 setup 建好的 client 传进来，调用 UpsertStart / UpsertEnd。
// 类型在 mooncake_pin_api.h。不包含 pyclient.h，不手写偏移。

#include "mooncake_pin_api.h"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstring>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace py = pybind11;

namespace {

constexpr const char* kCapsule = "mooncake.PyClient.shared_ptr";

struct Shared { void* ptr; void* ctrl; };

mooncake::Client* client_of(py::handle cap_obj) {
    py::capsule cap = py::cast<py::capsule>(cap_obj);
    if (std::strcmp(cap.name(), kCapsule) != 0 || cap.get_pointer() == nullptr) {
        throw std::runtime_error("没有已建 client，没有钉上");
    }
    auto* held = static_cast<Shared*>(cap.get_pointer());
    if (held->ptr == nullptr) {
        throw std::runtime_error("没有已建 client，没有钉上");
    }
    auto* py_client = static_cast<mooncake::PyClient*>(held->ptr);
    if (!py_client->client_) {
        throw std::runtime_error("已建 client 不可用，没有钉上");
    }
    return py_client->client_.get();
}

[[noreturn]] void fail(const std::string& msg,
                       const std::vector<std::string>& succeeded) {
    std::ostringstream os;
    os << msg;
    if (!succeeded.empty()) {
        os << "; 已钉上（不会回滚）: ";
        for (size_t i = 0; i < succeeded.size(); ++i) {
            if (i != 0) os << ",";
            os << succeeded[i];
        }
    }
    throw std::runtime_error(os.str());
}

}  // namespace

void empty_soft_pin(py::handle capsule, const std::vector<std::string>& keys,
                    const std::vector<size_t>& sizes,
                    const std::vector<uint64_t>& addresses, bool enable) {
    // sizes/addresses 来自零副作用查询。这里只允许 Case B：UpsertStart
    // 返回的 memory buffer 必须还是原来那一块；否则是 Case A/C（新
    // buffer/新 key），直接 Revoke，绝不 UpsertEnd。
    //
    // lease 只能挡驱逐和非 force Remove，挡不了 force Remove，也挡不了
    // Case C（目标版本 UpsertStart 只看 size）。但 Case C 的旧 buffer 会
    // 进 discarded_replicas_ 延迟释放（put_start_release_timeout，默认
    // 600s）；新 buffer 随后分配，旧 buffer 仍被持有，所以检查时不会
    // 立即复用旧地址。
    // 地址复用残余主要来自 Case A：调用方 exists 后 key 被 force remove /
    // stale 清理 / lease 过期驱逐，或 UpsertStart 开头的惰性清理 / processing
    // 抢占后没有 COMPLETE 副本；旧 buffer 真正释放后，新分配可能拿到同一
    // 地址和 size，此时地址检查会误判成 B。ABI 读错 descriptor 也会破坏检查。
    if (keys.size() != sizes.size() || keys.size() != addresses.size() ||
        keys.empty()) {
        throw std::runtime_error("软钉的 key/size/address 不合法，没有钉上");
    }
    auto* client = client_of(capsule);
    mooncake::ReplicateConfig config;
    config.replica_num = 1;
    config.soft_pin_action = enable ? mooncake::SoftPinAction::ENABLE
                                    : mooncake::SoftPinAction::DISABLE;
    std::vector<std::string> succeeded;
    // RPC 期间放开 GIL。这个作用域里只碰 C++ 字符串和 master client。
    py::gil_scoped_release release;
    for (size_t i = 0; i < keys.size(); ++i) {
        if (sizes[i] == 0 || addresses[i] == 0) {
            fail("软钉缺少原尺寸/地址，没有钉上: " + keys[i], succeeded);
        }
        auto started = client->master_client_.UpsertStart(
            keys[i], {sizes[i]}, config);
        if (!started.ok) {
            fail("UpsertStart 失败，没有钉上: " + keys[i], succeeded);
        }

        // Case B 的返回 descriptor 复用旧 buffer；Case A/C 会返回新
        // buffer。只对“唯一一个 memory descriptor 且地址/大小都等于旧值”
        // 放行，其余一律 Revoke。
        size_t memory_count = 0;
        bool matched = false;
        for (const auto& desc : started.value) {
            if (const auto* mem =
                    std::get_if<mooncake::MemDesc>(&desc.body)) {
                ++memory_count;
                if (static_cast<uint64_t>(
                        mem->buffer_descriptor.buffer_address_) == addresses[i] &&
                    static_cast<uint64_t>(mem->buffer_descriptor.size_) ==
                        sizes[i]) {
                    matched = true;
                }
            }
        }
        if (memory_count != 1 || !matched) {
            (void)client->master_client_.UpsertRevoke(
                keys[i], mooncake::ReplicaType::ALL);
            fail("UpsertStart 不是原位复用（Case A/C），已 Revoke，没有钉上: " +
                     keys[i],
                 succeeded);
        }

        auto ended = client->master_client_.UpsertEnd(
            mooncake::ObjectMeta{keys[i], std::nullopt},
            mooncake::ReplicaType::ALL);
        if (!ended.ok) {
            (void)client->master_client_.UpsertRevoke(
                keys[i], mooncake::ReplicaType::ALL);
            fail("UpsertEnd 失败，没有钉上: " + keys[i], succeeded);
        }
        succeeded.push_back(keys[i]);
    }
}

PYBIND11_MODULE(_empty_pin, m) {
    m.def("empty_soft_pin", &empty_soft_pin);
}
