// 只保留空写要用的类型。不包含 pyclient.h。
// Client 只写到 master_client_ 为止，后面的成员不影响它的位置。
// 与完整类定义对过（同一份源码树）：PyClient::client_ 在 8，
// Client::master_client_ 在 48，ReplicateConfig 208 字节，ObjectMeta 48 字节。

#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <variant>
#include <vector>

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

class ClientMetric;
class TransferEngine;

class MasterClient {
   public:
    CallResult<std::vector<Replica::Descriptor>> UpsertStart(
        const std::string& key, const std::vector<size_t>& slice_lengths,
        const ReplicateConfig& config);
    EndResult UpsertEnd(const ObjectMeta& meta, ReplicaType type);
    EndResult UpsertRevoke(const std::string& key, ReplicaType type);

   private:
    alignas(8) unsigned char storage_[8];
};

class Client {
   public:
    virtual ~Client();

   private:
    const UUID client_id_;
    std::unique_ptr<ClientMetric> metrics_;
    std::shared_ptr<TransferEngine> transfer_engine_;

   public:
    MasterClient master_client_;
};

class PyClient {
   public:
    virtual ~PyClient();
    std::shared_ptr<Client> client_;
};

}  // namespace mooncake
