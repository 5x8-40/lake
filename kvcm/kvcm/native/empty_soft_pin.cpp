// 软钉单独一个动态库。编译期不包含 Mooncake 头文件。
// 只声明要链的三个成员函数，链接官方 wheel 的 store.so。
// 符号名由编译器生成。已建 client 从 _get_pyclient_capsule 拿回。

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstdint>
#include <cstring>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace py = pybind11;

namespace mooncake {

enum class ErrorCode : int32_t {};
enum class ReplicaType : int { ALL = 4 };
enum class SoftPinAction : uint8_t { ENABLE = 1, DISABLE = 2 };
enum class ObjectDataType : uint8_t { UNKNOWN = 0 };
enum class ReplicaStatus : int { UNDEFINED = 0 };
using UUID = std::pair<uint64_t, uint64_t>;

struct BufferDesc {
    uint64_t size_{};
    uintptr_t buffer_address_{};
    std::string protocol_;
    std::string transport_endpoint_;
};
struct MemDesc { BufferDesc buffer_descriptor; };
struct NofDesc { BufferDesc buffer_descriptor; };
struct DiskDesc { std::string file_path; uint64_t object_size = 0; };
struct LocalDiskDesc {
    UUID client_id{};
    uint64_t object_size = 0;
    std::string transport_endpoint;
};
struct DfsDesc {
    std::string file_path;
    uint64_t offset = 0, object_size = 0, aligned_size = 0;
    int shard_idx = 0;
};

class Replica {
   public:
    struct Descriptor {
        uint64_t id{0};
        std::variant<MemDesc, NofDesc, DiskDesc, LocalDiskDesc, DfsDesc> body;
        ReplicaStatus status{ReplicaStatus::UNDEFINED};
    };
};

struct ObjectMeta {
    std::string key;
    std::optional<uint64_t> object_checksum;
};

struct ReplicateConfig {
    size_t replica_num{1};
    size_t nof_replica_num{0};
    size_t dfs_replica_num{0};
    SoftPinAction soft_pin_action{SoftPinAction::ENABLE};
    std::optional<uint64_t> soft_pin_ttl_ms{};
    bool with_hard_pin{false};
    std::vector<std::string> preferred_segments{};
    std::string preferred_segment{};
    std::vector<std::string> preferred_nof_segments{};
    bool prefer_alloc_in_same_node{false};
    ObjectDataType data_type{ObjectDataType::UNKNOWN};
    std::string host_id{};
    std::optional<std::vector<std::string>> group_ids{};
};

template <class T>
struct CallResult {
    union {
        T value;
        ErrorCode error;
        char empty;
    };
    bool ok{false};
    CallResult() : empty(0) {}
    ~CallResult() { if (ok) value.~T(); }
    CallResult(const CallResult&) = delete;
    CallResult& operator=(const CallResult&) = delete;
};

struct EndResult {
    ErrorCode error{};
    bool ok{false};
    ~EndResult() {}
};

class MasterClient {
   public:
    CallResult<std::vector<Replica::Descriptor>> UpsertStart(
        const std::string& key, const std::vector<size_t>& slice_lengths,
        const ReplicateConfig& config);
    EndResult UpsertEnd(const ObjectMeta& object_meta, ReplicaType replica_type);
    EndResult UpsertRevoke(const std::string& key, ReplicaType replica_type);
};

}  // namespace mooncake

namespace {

constexpr const char* kCapsule = "mooncake.PyClient.shared_ptr";

struct Shared { void* ptr; void* ctrl; };

void* master_of(py::handle cap_obj) {
    py::capsule cap = py::cast<py::capsule>(cap_obj);
    if (std::strcmp(cap.name(), kCapsule) != 0 || cap.get_pointer() == nullptr) {
        throw std::runtime_error("没有已建 client，没有钉上");
    }
    auto* held = static_cast<Shared*>(cap.get_pointer());
    auto* client_sp =
        reinterpret_cast<Shared*>(static_cast<char*>(held->ptr) + sizeof(void*));
    if (held->ptr == nullptr || client_sp->ptr == nullptr) {
        throw std::runtime_error("已建 client 不可用，没有钉上");
    }
    // Client：虚表、UUID、metrics_、transfer_engine_，然后是 master_client_。
    constexpr size_t kMaster =
        sizeof(void*) + sizeof(mooncake::UUID) + sizeof(void*) + sizeof(Shared);
    return static_cast<char*>(client_sp->ptr) + kMaster;
}

}  // namespace

void empty_soft_pin(py::handle capsule, const std::vector<std::string>& keys,
                    const std::vector<size_t>& sizes, bool enable) {
    if (keys.size() != sizes.size() || keys.empty()) {
        throw std::runtime_error("软钉的 key 和 size 不合法，没有钉上");
    }
    auto* master = static_cast<mooncake::MasterClient*>(master_of(capsule));
    mooncake::ReplicateConfig config;
    config.replica_num = 1;
    config.soft_pin_action = enable ? mooncake::SoftPinAction::ENABLE
                                    : mooncake::SoftPinAction::DISABLE;
    for (size_t i = 0; i < keys.size(); ++i) {
        if (sizes[i] == 0) {
            throw std::runtime_error("软钉缺少原尺寸，没有钉上: " + keys[i]);
        }
        auto started = master->UpsertStart(keys[i], {sizes[i]}, config);
        if (!started.ok) {
            throw std::runtime_error("UpsertStart 失败，没有钉上: " + keys[i]);
        }
        auto ended = master->UpsertEnd(
            mooncake::ObjectMeta{keys[i], std::nullopt}, mooncake::ReplicaType::ALL);
        if (!ended.ok) {
            (void)master->UpsertRevoke(keys[i], mooncake::ReplicaType::ALL);
            throw std::runtime_error("UpsertEnd 失败，没有钉上: " + keys[i]);
        }
    }
}

PYBIND11_MODULE(_empty_pin, m) {
    m.def("empty_soft_pin", &empty_soft_pin);
}
