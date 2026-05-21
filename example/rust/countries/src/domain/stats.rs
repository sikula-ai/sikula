use crate::domain::country::Country;

#[derive(Debug)]
pub struct RegionSummary {
    pub region: String,
    pub country_count: usize,
    pub total_population: u64,
}

#[derive(Debug)]
pub struct Stats {
    pub total_countries: usize,
    pub total_population: u64,
    pub largest_by_area: Option<String>,
    pub smallest_by_area: Option<String>,
    pub most_populous: Option<String>,
    pub least_populous: Option<String>,
    pub regions: Vec<RegionSummary>,
}

/// Computes aggregate statistics over the provided slice of countries.
pub fn compute(countries: &[Country]) -> Stats {
    if countries.is_empty() {
        return Stats {
            total_countries: 0,
            total_population: 0,
            largest_by_area: None,
            smallest_by_area: None,
            most_populous: None,
            least_populous: None,
            regions: vec![],
        };
    }

    let total_population = countries.iter().map(|c| c.population).sum();

    // Ordered floats cannot use f64's built-in Ord, so we compare manually.
    let largest_by_area = countries
        .iter()
        .max_by(|a, b| {
            a.area
                .partial_cmp(&b.area)
                .unwrap_or(std::cmp::Ordering::Equal)
        })
        .map(|c| c.name.common.clone());

    let smallest_by_area = countries
        .iter()
        .min_by(|a, b| {
            a.area
                .partial_cmp(&b.area)
                .unwrap_or(std::cmp::Ordering::Equal)
        })
        .map(|c| c.name.common.clone());

    let most_populous = countries
        .iter()
        .max_by_key(|c| c.population)
        .map(|c| c.name.common.clone());

    let least_populous = countries
        .iter()
        .min_by_key(|c| c.population)
        .map(|c| c.name.common.clone());

    let regions = region_summaries(countries);

    Stats {
        total_countries: countries.len(),
        total_population,
        largest_by_area,
        smallest_by_area,
        most_populous,
        least_populous,
        regions,
    }
}

/// Groups countries by region and computes per-region totals.
fn region_summaries(countries: &[Country]) -> Vec<RegionSummary> {
    // Collect unique region names while preserving insertion order.
    let mut seen: Vec<String> = Vec::new();
    for c in countries {
        if !seen.contains(&c.region) {
            seen.push(c.region.clone());
        }
    }
    seen.sort_unstable();

    seen.into_iter()
        .map(|region| {
            let matching: Vec<&Country> = countries.iter().filter(|c| c.region == region).collect();
            RegionSummary {
                total_population: matching.iter().map(|c| c.population).sum(),
                country_count: matching.len(),
                region,
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::country::{Country, CountryName, Currency};
    use std::collections::HashMap;

    fn make_country(name: &str, region: &str, population: u64, area: f64, code: &str) -> Country {
        Country {
            name: CountryName {
                common: name.to_owned(),
                official: name.to_owned(),
            },
            cca3: code.parse().unwrap(),
            capital: vec![],
            region: region.to_owned(),
            subregion: String::new(),
            population,
            area,
            languages: HashMap::new(),
            currencies: {
                let mut m = HashMap::new();
                m.insert(
                    "TST".to_owned(),
                    Currency {
                        name: "Test".to_owned(),
                        symbol: "T".to_owned(),
                    },
                );
                m
            },
        }
    }

    fn sample() -> Vec<Country> {
        vec![
            make_country("Alpha", "Europe", 1_000_000, 100_000.0, "ALP"),
            make_country("Beta", "Europe", 5_000_000, 50_000.0, "BET"),
            make_country("Gamma", "Asia", 200_000_000, 9_000_000.0, "GAM"),
        ]
    }

    #[test]
    fn total_population_sum() {
        let s = compute(&sample());
        assert_eq!(s.total_population, 206_000_000);
    }

    #[test]
    fn total_country_count() {
        let s = compute(&sample());
        assert_eq!(s.total_countries, 3);
    }

    #[test]
    fn largest_by_area() {
        let s = compute(&sample());
        assert_eq!(s.largest_by_area.as_deref(), Some("Gamma"));
    }

    #[test]
    fn smallest_by_area() {
        let s = compute(&sample());
        assert_eq!(s.smallest_by_area.as_deref(), Some("Beta"));
    }

    #[test]
    fn most_populous() {
        let s = compute(&sample());
        assert_eq!(s.most_populous.as_deref(), Some("Gamma"));
    }

    #[test]
    fn least_populous() {
        let s = compute(&sample());
        assert_eq!(s.least_populous.as_deref(), Some("Alpha"));
    }

    #[test]
    fn region_summaries_correct() {
        let s = compute(&sample());
        let europe = s.regions.iter().find(|r| r.region == "Europe").unwrap();
        assert_eq!(europe.country_count, 2);
        assert_eq!(europe.total_population, 6_000_000);

        let asia = s.regions.iter().find(|r| r.region == "Asia").unwrap();
        assert_eq!(asia.country_count, 1);
        assert_eq!(asia.total_population, 200_000_000);
    }

    #[test]
    fn empty_input_returns_zero_stats() {
        let s = compute(&[]);
        assert_eq!(s.total_countries, 0);
        assert_eq!(s.total_population, 0);
        assert!(s.largest_by_area.is_none());
    }
}
