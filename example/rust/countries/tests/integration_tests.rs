use std::path::PathBuf;

// Resolve the data file relative to the workspace root so tests work regardless
// of the working directory used by `cargo test`.
fn data_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data/countries.json")
}

mod loader_tests {
    use super::*;
    use countries::data::loader::load_countries;

    #[test]
    fn loads_expected_country_count() {
        let countries = load_countries(&data_path()).expect("should load countries.json");
        // The data file contains 25 countries; assert at least that many exist
        // so the test doesn't break if new entries are added later.
        assert!(
            countries.len() >= 25,
            "expected at least 25 countries, got {}",
            countries.len()
        );
    }

    #[test]
    fn all_entries_have_non_empty_names() {
        let countries = load_countries(&data_path()).expect("should load countries.json");
        for c in &countries {
            assert!(
                !c.name.common.is_empty(),
                "country has empty common name: {:?}",
                c.cca3
            );
        }
    }

    #[test]
    fn all_codes_are_three_letters() {
        let countries = load_countries(&data_path()).expect("should load countries.json");
        for c in &countries {
            assert_eq!(
                c.cca3.as_str().len(),
                3,
                "expected 3-letter code, got: {}",
                c.cca3
            );
        }
    }
}

mod filter_tests {
    use super::*;
    use countries::data::loader::load_countries;
    use countries::domain::filter;

    #[test]
    fn region_filter_returns_only_europe() {
        let countries = load_countries(&data_path()).expect("should load countries.json");
        let europe = filter::filter_by_region(&countries, "Europe");
        assert!(!europe.is_empty(), "expected at least one European country");
        for c in europe {
            assert_eq!(c.region, "Europe", "unexpected region: {}", c.region);
        }
    }

    #[test]
    fn region_filter_case_insensitive() {
        let countries = load_countries(&data_path()).expect("should load countries.json");
        let lower = filter::filter_by_region(&countries, "europe");
        let upper = filter::filter_by_region(&countries, "Europe");
        assert_eq!(lower.len(), upper.len());
    }

    #[test]
    fn search_finds_czech_republic() {
        let countries = load_countries(&data_path()).expect("should load countries.json");
        let results = filter::search_by_name(&countries, "Czech");
        assert!(
            !results.is_empty(),
            "expected to find Czech Republic by partial name"
        );
        assert!(results.iter().any(|c| c.name.common == "Czech Republic"));
    }

    #[test]
    fn find_by_code_cze() {
        let countries = load_countries(&data_path()).expect("should load countries.json");
        let code: countries::domain::country::CountryCode = "CZE".parse().unwrap();
        let result = filter::find_by_code(&countries, &code);
        assert!(result.is_some());
        assert_eq!(result.unwrap().name.common, "Czech Republic");
    }
}

mod stats_tests {
    use super::*;
    use countries::data::loader::load_countries;
    use countries::domain::stats;

    #[test]
    fn global_stats_total_countries_matches_loaded() {
        let countries = load_countries(&data_path()).expect("should load countries.json");
        let s = stats::compute(&countries);
        assert_eq!(s.total_countries, countries.len());
    }

    #[test]
    fn global_stats_population_positive() {
        let countries = load_countries(&data_path()).expect("should load countries.json");
        let s = stats::compute(&countries);
        assert!(s.total_population > 1_000_000_000);
    }

    #[test]
    fn global_stats_has_largest_country() {
        let countries = load_countries(&data_path()).expect("should load countries.json");
        let s = stats::compute(&countries);
        assert!(s.largest_by_area.is_some());
    }
}
