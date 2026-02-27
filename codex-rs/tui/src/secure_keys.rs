use std::path::Path;

use anyhow::Result;
use codex_secrets::SecretName;
use codex_secrets::SecretScope;
use codex_secrets::SecretsBackendKind;
use codex_secrets::SecretsManager;

fn secret_name(env_var: &str) -> Result<SecretName> {
    SecretName::new(env_var)
}

pub(crate) fn key_is_set(codex_home: &Path, env_var: &str) -> Result<bool> {
    let manager = SecretsManager::new(codex_home.to_path_buf(), SecretsBackendKind::Local);
    let name = secret_name(env_var)?;
    Ok(manager.get(&SecretScope::Global, &name)?.is_some())
}

pub(crate) fn set_key(codex_home: &Path, env_var: &str, value: &str) -> Result<()> {
    let manager = SecretsManager::new(codex_home.to_path_buf(), SecretsBackendKind::Local);
    let name = secret_name(env_var)?;
    manager.set(&SecretScope::Global, &name, value)
}

pub(crate) fn clear_key(codex_home: &Path, env_var: &str) -> Result<bool> {
    let manager = SecretsManager::new(codex_home.to_path_buf(), SecretsBackendKind::Local);
    let name = secret_name(env_var)?;
    manager.delete(&SecretScope::Global, &name)
}
