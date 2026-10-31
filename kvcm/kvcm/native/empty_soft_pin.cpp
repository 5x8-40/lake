// 把 setup 建好的 client 传进来，调用 UpsertStart / UpsertEnd。
// 类型在 mooncake_pin_api.h。不包含 pyclient.h，不手写偏移。

#include "mooncake_pin_api.h"

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstring>
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

}  // namespace

void empty_soft_pin(py::handle capsule, const std::vector<std::string>& keys,
                    const std::vector<size_t>& sizes, bool enable) {
    if (keys.size() != sizes.size() || keys.empty()) {
        throw std::runtime_error("软钉的 key 和 size 不合法，没有钉上");
    }
    auto* client = client_of(capsule);
    mooncake::ReplicateConfig config;
    config.replica_num = 1;
    config.soft_pin_action = enable ? mooncake::SoftPinAction::ENABLE
                                    : mooncake::SoftPinAction::DISABLE;
    for (size_t i = 0; i < keys.size(); ++i) {
        if (sizes[i] == 0) {
            throw std::runtime_error("软钉缺少原尺寸，没有钉上: " + keys[i]);
        }
        auto started = client->master_client_.UpsertStart(
            keys[i], {sizes[i]}, config);
        if (!started.ok) {
            throw std::runtime_error("UpsertStart 失败，没有钉上: " + keys[i]);
        }
        auto ended = client->master_client_.UpsertEnd(
            mooncake::ObjectMeta{keys[i], std::nullopt}, mooncake::ReplicaType::ALL);
        if (!ended.ok) {
            (void)client->master_client_.UpsertRevoke(keys[i], mooncake::ReplicaType::ALL);
            throw std::runtime_error("UpsertEnd 失败，没有钉上: " + keys[i]);
        }
    }
}

PYBIND11_MODULE(_empty_pin, m) {
    m.def("empty_soft_pin", &empty_soft_pin);
}
