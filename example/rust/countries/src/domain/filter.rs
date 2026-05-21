use crate::domain::country::{Country, CountryCode};

/// Returns countries whose common name contains `query` (case-insensitive).
pub fn search_by_name<'a>(countries: &'a [Country], query: &str) -> Vec<&'a Country> {
    let lower = query.to_lowercase();
    countries
        .iter()
        .filter(|c| c.name.common.to_lowercase().contains(&lower))
        .collect()
}

/// Returns countries belonging to the given region (case-insensitive exact match).
pub fn filter_by_region<'a>(countries: &'a [Country], region: &str) -> Vec<&'a Country> {
    let lower = region.to_lowercase();
    countries
        .iter()
        .filter(|c| c.region.to_lowercase() == lower)
        .collect()
}

/// Looks up a single country by its alpha-3 code.
pub fn find_by_code<'a>(countries: &'a [Country], code: &CountryCode) -> Option<&'a Country> {
    countries.iter().find(|c| &c.cca3 == code)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::country::{Country, CountryName, Currency};
    use std::collections::HashMap;

    fn make_country(common: &str, region: &str, code: &str) -> Country {
        Country {
            name: CountryName {
                common: common.to_owned(),
                official: common.to_owned(),
            },
            cca3: code.parse().unwrap(),
            capital: vec!["Capital".to_owned()],
            region: region.to_owned(),
            subregion: String::new(),
            population: 1_000_000,
            area: 1000.0,
            languages: HashMap::new(),
            currencies: {
                let mut m = HashMap::new();
                m.insert(
                    "TST".to_owned(),
                    Currency {
                        name: "Test currency".to_owned(),
                        symbol: "T".to_owned(),
                    },
                );
                m
            },
        }
    }

    fn sample() -> Vec<Country> {
        vec![
            make_country("Czech Republic", "Europe", "CZE"),
            make_country("Germany", "Europe", "DEU"),
            make_country("Japan", "Asia", "JPN"),
            make_country("Brazil", "Americas", "BRA"),
        ]
    }

    #[test]
    fn search_exact_match() {
        let countries = sample();
        let results = search_by_name(&countries, "Japan");
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].name.common, "Japan");
    }

    #[test]
    fn search_partial_match() {
        let countries = sample();
        let results = search_by_name(&countries, "Czec");
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].name.common, "Czech Republic");
    }

    #[test]
    fn search_case_insensitive() {
        let countries = sample();
        let results = search_by_name(&countries, "germany");
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].name.common, "Germany");
    }

    #[test]
    fn search_no_match() {
        let countries = sample();
        let results = search_by_name(&countries, "Narnia");
        assert!(results.is_empty());
    }

    #[test]
    fn search_multiple_matches() {
        let countries = sample();
        // Both "Czech Republic" and "Germany" contain "e"
        let results = search_by_name(&countries, "e");
        assert!(results.len() >= 2);
    }

    #[test]
    fn filter_region_returns_only_that_region() {
        let countries = sample();
        let results = filter_by_region(&countries, "Europe");
        assert_eq!(results.len(), 2);
        assert!(results.iter().all(|c| c.region == "Europe"));
    }

    #[test]
    fn filter_region_case_insensitive() {
        let countries = sample();
        let results = filter_by_region(&countries, "europe");
        assert_eq!(results.len(), 2);
    }

    #[test]
    fn filter_region_no_match() {
        let countries = sample();
        let results = filter_by_region(&countries, "Oceania");
        assert!(results.is_empty());
    }

    #[test]
    fn find_by_code_found() {
        let countries = sample();
        let code: CountryCode = "JPN".parse().unwrap();
        let result = find_by_code(&countries, &code);
        assert!(result.is_some());
        assert_eq!(result.unwrap().name.common, "Japan");
    }

    #[test]
    fn find_by_code_not_found() {
        let countries = sample();
        let code: CountryCode = "ZZZ".parse().unwrap();
        assert!(find_by_code(&countries, &code).is_none());
    }
}
