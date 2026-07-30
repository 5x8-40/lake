//! P4.7:降频 checkpoint 存储抽象。
//!
//! 权威仍在进程内存;本层只做崩溃重建后盾。P4 = in-memory mock;
//! 真 etcd 接入留 P6(见 `docs/architecture/control-plane.md`)。
//!
//! 参考:Mooncake HA snapshot / OpLog 是另一套复制模型;lake 只要求
//! 「节点/模型/配额 + 位置快照」可 load,不做 Raft。

use std::sync::Mutex;

use lake_proto::lake::CheckpointSnapshot;

pub trait CheckpointStore: Send + Sync {
    fn save(&self, snap: CheckpointSnapshot) -> Result<(), String>;
    fn load(&self) -> Result<Option<CheckpointSnapshot>, String>;
}

/// Process-local mock (单测 / P4 单进程)。
#[derive(Default)]
pub struct MemoryCheckpointStore {
    inner: Mutex<Option<CheckpointSnapshot>>,
}

impl MemoryCheckpointStore {
    pub fn new() -> Self {
        Self::default()
    }
}

impl CheckpointStore for MemoryCheckpointStore {
    fn save(&self, snap: CheckpointSnapshot) -> Result<(), String> {
        *self.inner.lock().map_err(|e| e.to_string())? = Some(snap);
        Ok(())
    }

    fn load(&self) -> Result<Option<CheckpointSnapshot>, String> {
        Ok(self.inner.lock().map_err(|e| e.to_string())?.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use lake_proto::lake::ModelDescriptor;

    #[test]
    fn memory_roundtrip() {
        let store = MemoryCheckpointStore::new();
        assert!(store.load().unwrap().is_none());
        let snap = CheckpointSnapshot {
            seq: 7,
            models: vec![ModelDescriptor {
                model_id: "m".into(),
                ..Default::default()
            }],
            blocks: Vec::new(),
        };
        store.save(snap.clone()).unwrap();
        let got = store.load().unwrap().unwrap();
        assert_eq!(got.seq, 7);
        assert_eq!(got.models[0].model_id, "m");
    }
}
