use crate::cli::ListArgs;
use countries::domain::country::Country;
use countries::domain::filter;
use countries::error::AppError;

const NAME_W: usize = 30;
const CODE_W: usize = 5;
const CAPITAL_W: usize = 20;
const REGION_W: usize = 12;

pub fn run(args: &ListArgs, countries: &[Country]) -> Result<(), AppError> {
    let filtered: Vec<&Country> = match &args.region {
        Some(region) => filter::filter_by_region(countries, region),
        None => countries.iter().collect(),
    };

    if filtered.is_empty() {
        return Err(AppError::NoMatchingCountries);
    }

    print_header();
    for country in filtered {
        print_row(country);
    }

    Ok(())
}

fn print_header() {
    println!(
        "{:<NAME_W$}  {:<CODE_W$}  {:<CAPITAL_W$}  {:<REGION_W$}  POPULATION",
        "NAME", "CODE", "CAPITAL", "REGION",
    );
    println!(
        "{}",
        "-".repeat(NAME_W + CODE_W + CAPITAL_W + REGION_W + 22)
    );
}

fn print_row(c: &Country) {
    println!(
        "{:<NAME_W$}  {:<CODE_W$}  {:<CAPITAL_W$}  {:<REGION_W$}  {}",
        truncate(&c.name.common, NAME_W),
        c.cca3,
        truncate(c.primary_capital(), CAPITAL_W),
        truncate(&c.region, REGION_W),
        format_population(c.population),
    );
}

fn truncate(s: &str, max: usize) -> String {
    let chars: Vec<char> = s.chars().collect();
    if chars.len() <= max {
        s.to_owned()
    } else {
        // Reserve one char slot for the ellipsis.
        let truncated: String = chars[..max.saturating_sub(1)].iter().collect();
        format!("{truncated}…")
    }
}

fn format_population(n: u64) -> String {
    // Insert thousands separators manually to avoid external formatting crates.
    let s = n.to_string();
    let mut result = String::with_capacity(s.len() + s.len() / 3);
    for (i, ch) in s.chars().rev().enumerate() {
        if i > 0 && i % 3 == 0 {
            result.push(',');
        }
        result.push(ch);
    }
    result.chars().rev().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn format_population_thousands() {
        assert_eq!(format_population(1_000_000), "1,000,000");
    }

    #[test]
    fn format_population_small() {
        assert_eq!(format_population(42), "42");
    }
}
