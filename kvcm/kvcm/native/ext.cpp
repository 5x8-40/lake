// 自封装 .so（路径 A）：复用 store_py 已建的 MooncakeStorePyWrapper。
//
// 不在本仓库编译，不改 3rdparty/mooncake。链上官方 wheel 之后才替换
// 下面的报错。未链上时两个入口都抛异常，不能被当成已经钉上或已经保留。
//
// 打算调用的符号（同一 RealClient，不新建 client）：
//   MooncakeStorePyWrapper::get_real_client()
//   空写：BatchUpsertStart / BatchUpsertEnd（带 soft pin，不写数据）
//   保留身份：把该 client 登记为 retaining client
//     （MasterService::AddReplicaForRetainedClient 所在的那条身份）

#include <stdexcept>
#include <string>
#include <vector>

namespace {

[[noreturn]] void not_linked(const char* what) {
    throw std::runtime_error(
        std::string("kvcm_native: ") + what +
        " 未链上 mooncake wheel，没有执行");
}

}  // namespace

// 给以后的 pybind 模块留的 C++ 入口。Python 侧函数名与此一致。
int soft_pin_empty_upsert(void* /*store*/,
                          const std::vector<std::string>& /*keys*/) {
    not_linked("软钉空写");
}

int register_retained_client(void* /*store*/) {
    not_linked("保留身份");
}
