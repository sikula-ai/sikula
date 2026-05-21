use std::collections::HashMap;
use std::fmt;
use std::str::FromStr;

use serde::Deserialize;

use crate::error::AppError;

/// A validated ISO 3166-1 alpha-3 country code, stored in uppercase.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Deserialize)]
#[serde(try_from = "String")]
pub struct CountryCode(String);

impl CountryCode {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for CountryCode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl FromStr for CountryCode {
    type Err = AppError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        let upper = s.trim().to_uppercase();
        if upper.len() == 3 && upper.bytes().all(|b| b.is_ascii_alphabetic()) {
            Ok(CountryCode(upper))
        } else {
            Err(AppError::InvalidCountryCode(s.to_owned()))
        }
    }
}

// Allows serde to reuse the FromStr validation when deserializing.
impl TryFrom<String> for CountryCode {
    type Error = AppError;

    fn try_from(value: String) -> Result<Self, Self::Error> {
        value.parse()
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct CountryName {
    pub common: String,
    pub official: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Currency {
    pub name: String,
    pub symbol: String,
}

/// A country as modelled by the application.
///
/// Fields mirror the REST Countries API subset stored in `data/countries.json`.
/// Serde handles deserialization; no I/O lives here.
#[derive(Debug, Clone, Deserialize)]
pub struct Country {
    pub name: CountryName,
    pub cca3: CountryCode,
    pub capital: Vec<String>,
    pub region: String,
    pub subregion: String,
    pub population: u64,
    pub area: f64,
    pub languages: HashMap<String, String>,
    pub currencies: HashMap<String, Currency>,
}

impl Country {
    /// The primary capital city, or `"—"` when the list is empty.
    pub fn primary_capital(&self) -> &str {
        self.capital.first().map(String::as_str).unwrap_or("—")
    }

    /// A comma-separated list of official language names.
    pub fn language_names(&self) -> String {
        let mut names: Vec<&str> = self.languages.values().map(String::as_str).collect();
        names.sort_unstable();
        names.join(", ")
    }

    /// A comma-separated list of currency descriptions (`name (symbol)`).
    pub fn currency_summary(&self) -> String {
        let mut parts: Vec<String> = self
            .currencies
            .values()
            .map(|c| format!("{} ({})", c.name, c.symbol))
            .collect();
        parts.sort_unstable();
        parts.join(", ")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_code(s: &str) -> Result<CountryCode, AppError> {
        s.parse()
    }

    #[test]
    fn valid_code_uppercase() {
        let code = make_code("CZE").unwrap();
        assert_eq!(code.as_str(), "CZE");
    }

    #[test]
    fn valid_code_lowercase_normalized() {
        let code = make_code("cze").unwrap();
        assert_eq!(code.as_str(), "CZE");
    }

    #[test]
    fn valid_code_mixed_case_normalized() {
        let code = make_code("Cze").unwrap();
        assert_eq!(code.as_str(), "CZE");
    }

    #[test]
    fn invalid_code_too_short() {
        assert!(make_code("CZ").is_err());
    }

    #[test]
    fn invalid_code_too_long() {
        assert!(make_code("CZEC").is_err());
    }

    #[test]
    fn invalid_code_contains_digit() {
        assert!(make_code("C3E").is_err());
    }

    #[test]
    fn invalid_code_empty() {
        assert!(make_code("").is_err());
    }

    #[test]
    fn display_round_trips() {
        let code = make_code("deu").unwrap();
        assert_eq!(code.to_string(), "DEU");
    }
}
