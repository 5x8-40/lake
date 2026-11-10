// 把 setup 建好的 client 传进来，调用 GetReplicaList / UpsertStart / UpsertEnd。
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

// 从 descriptor 列表里取出唯一 memory 副本；允许多余 NoF。遇盘/DFS 拒绝。
bool find_sole_memory(const std::vector<mooncake::Replica::Descriptor>& descs,
                      uint64_t* id_out, uint64_t* addr_out, uint64_t* size_out,
                      std::string* err) {
    size_t memory_count = 0;
    uint64_t id = 0;
    uint64_t addr = 0;
    uint64_t size = 0;
    for (const auto& desc : descs) {
        if (const auto* mem = std::get_if<mooncake::MemDesc>(&desc.body)) {
            ++memory_count;
            id = desc.id;
            addr = static_cast<uint64_t>(mem->buffer_descriptor.buffer_address_);
            size = static_cast<uint64_t>(mem->buffer_descriptor.size_);
        } else if (std::get_if<mooncake::NofDesc>(&desc.body)) {
            // memory+NoF 允许；UpsertEnd(ALL) 含 nof。
            continue;
        } else {
            *err = "有盘/DFS 副本，空写收尾盖不住";
            return false;
        }
    }
    if (memory_count != 1) {
        *err = "需要恰好一块 memory 副本，实际=" + std::to_string(memory_count);
        return false;
    }
    *id_out = id;
    *addr_out = addr;
    *size_out = size;
    return true;
}

}  // namespace

void empty_soft_pin(py::handle capsule, const std::vector<std::string>& keys,
                    const std::vector<size_t>& sizes,
                    const std::vector<uint64_t>& addresses, bool enable) {
    // 方案 1 + replica id（关 A15，见 D2 §2）：
    // 1) GetReplicaList 取旧 memory ReplicaID（并授 lease）；
    // 2) UpsertStart；
    // 3) 返回 id 与旧 id 相同 → Case B，只 End（End 失败不 Revoke）；
    //    id 不同 → Case A/C，Revoke，绝不 End。
    // 地址/size：Start 前与 list/调用方比对；Start 后 id 已同时信任 End，
    // 不作 Revoke 依据。Start 返回形状不明 → 失败且不 Revoke。
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
    py::gil_scoped_release release;
    for (size_t i = 0; i < keys.size(); ++i) {
        if (sizes[i] == 0 || addresses[i] == 0) {
            fail("软钉缺少原尺寸/地址，没有钉上: " + keys[i], succeeded);
        }

        auto listed = client->master_client_.GetReplicaList(keys[i]);
        if (!listed.ok) {
            fail("GetReplicaList 失败，没有钉上: " + keys[i], succeeded);
        }
        uint64_t old_id = 0;
        uint64_t listed_addr = 0;
        uint64_t listed_size = 0;
        std::string shape_err;
        if (!find_sole_memory(listed.value.replicas, &old_id, &listed_addr,
                              &listed_size, &shape_err)) {
            fail("GetReplicaList " + shape_err + "，没有钉上: " + keys[i],
                 succeeded);
        }
        if (listed_addr != addresses[i] || listed_size != sizes[i]) {
            fail("GetReplicaList 地址/尺寸与调用方不一致，没有钉上: " + keys[i],
                 succeeded);
        }
        if (old_id == 0) {
            fail("GetReplicaList 未给出 replica id，没有钉上: " + keys[i],
                 succeeded);
        }

        auto started = client->master_client_.UpsertStart(
            keys[i], {sizes[i]}, config);
        if (!started.ok) {
            fail("UpsertStart 失败，没有钉上: " + keys[i], succeeded);
        }

        uint64_t new_id = 0;
        uint64_t new_addr = 0;
        uint64_t new_size = 0;
        if (!find_sole_memory(started.value, &new_id, &new_addr, &new_size,
                              &shape_err)) {
            // 形状不明时可能仍是 Case B；无条件 Revoke 会 Erase 好数据。
            // 失败且不 Revoke（副本可能仍 PROCESSING）。
            fail("UpsertStart " + shape_err + "，未 Revoke，没有钉上: " +
                     keys[i],
                 succeeded);
        }

        // 主判据：replica id。只有 id 不匹配才 Revoke（Case A/C）。
        // Start 后地址/size 不再作 Revoke 依据（id 同 → 信任 Case B 走 End）。
        (void)new_addr;
        (void)new_size;
        if (new_id != old_id) {
            auto revoked = client->master_client_.UpsertRevoke(
                keys[i], mooncake::ReplicaType::ALL);
            if (!revoked.ok) {
                fail("UpsertStart replica id 不匹配（Case A/C），Revoke 也失败，"
                     "副本可能仍 PROCESSING，没有钉上: " +
                         keys[i],
                     succeeded);
            }
            fail("UpsertStart replica id 不匹配（Case A/C），已 Revoke，"
                 "没有钉上: " +
                     keys[i],
                 succeeded);
        }

        auto ended = client->master_client_.UpsertEnd(
            mooncake::ObjectMeta{keys[i], std::nullopt},
            mooncake::ReplicaType::ALL);
        if (!ended.ok) {
            // 已确认 Case B（id 匹配），禁止 Revoke（会 Erase 好数据）。
            fail("UpsertEnd 失败（Case B，未 Revoke，副本可能仍 PROCESSING）"
                 "，没有钉上: " +
                     keys[i],
                 succeeded);
        }
        succeeded.push_back(keys[i]);
    }
}

namespace {

// 与 A14 / empty_pin.EXPECTED_ABI_LAYOUT 对齐；改 mooncake_pin_api.h 时同步改 Python 常量。
py::dict abi_layout() {
    py::dict out;
    out["ObjectMeta"] = static_cast<int>(sizeof(mooncake::ObjectMeta));
    out["ReplicateConfig"] = static_cast<int>(sizeof(mooncake::ReplicateConfig));
    out["EndResult"] = static_cast<int>(sizeof(mooncake::EndResult));
    out["PyClient.client_"] =
        static_cast<int>(offsetof(mooncake::PyClient, client_));
    out["Client.master_client_"] =
        static_cast<int>(offsetof(mooncake::Client, master_client_));
    return out;
}

}  // namespace

PYBIND11_MODULE(_empty_pin, m) {
    m.def("empty_soft_pin", &empty_soft_pin);
    m.def("abi_layout", &abi_layout);
}
