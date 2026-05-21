use crate::cli::SearchArgs;
use countries::domain::country::Country;
use countries::domain::filter;
use countries::error::AppError;

pub fn run(args: &SearchArgs, countries: &[Country]) -> Result<(), AppError> {
    let results = filter::search_by_name(countries, &args.name);

    if results.is_empty() {
        return Err(AppError::NoMatchingCountries);
    }

    println!(
        "{:<30}  {:<6}  {:<12}  POPULATION",
        "NAME", "CODE", "REGION"
    );
    println!("{}", "-".repeat(68));

    for c in results {
        println!(
            "{:<30}  {:<6}  {:<12}  {}",
            c.name.common,
            c.cca3,
            c.region,
            format_population(c.population),
        );
    }

    Ok(())
}

fn format_population(n: u64) -> String {
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
