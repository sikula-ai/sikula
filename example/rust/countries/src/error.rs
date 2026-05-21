use thiserror::Error;

#[derive(Debug, Error)]
pub enum AppError {
    #[error("failed to load data file '{path}': {source}")]
    DataLoad {
        path: String,
        #[source]
        source: std::io::Error,
    },

    #[error("failed to parse data file '{path}': {source}")]
    DataParse {
        path: String,
        #[source]
        source: serde_json::Error,
    },

    #[error("country not found: '{0}'")]
    CountryNotFound(String),

    #[error("invalid country code '{0}': must be exactly 3 ASCII letters")]
    InvalidCountryCode(String),

    #[error("no countries match the given filter")]
    NoMatchingCountries,
}
