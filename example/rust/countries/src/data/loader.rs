use std::path::Path;

use crate::domain::country::Country;
use crate::error::AppError;

/// Reads and parses the countries JSON array from `path`.
///
/// Returns an `AppError` with the path embedded so callers can report a clear
/// message without needing to know the file location themselves.
pub fn load_countries(path: &Path) -> Result<Vec<Country>, AppError> {
    let contents = std::fs::read_to_string(path).map_err(|source| AppError::DataLoad {
        path: path.display().to_string(),
        source,
    })?;

    serde_json::from_str(&contents).map_err(|source| AppError::DataParse {
        path: path.display().to_string(),
        source,
    })
}
