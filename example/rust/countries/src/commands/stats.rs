use crate::cli::StatsArgs;
use countries::domain::country::Country;
use countries::domain::filter;
use countries::domain::stats;
use countries::error::AppError;

pub fn run(args: &StatsArgs, countries: &[Country]) -> Result<(), AppError> {
    let subset: Vec<&Country> = match &args.region {
        Some(region) => filter::filter_by_region(countries, region),
        None => countries.iter().collect(),
    };

    if subset.is_empty() {
        return Err(AppError::NoMatchingCountries);
    }

    // Collect owned copies so stats::compute can borrow a contiguous slice.
    let owned: Vec<Country> = subset.into_iter().cloned().collect();
    let s = stats::compute(&owned);

    if let Some(region) = &args.region {
        println!("Statistics for region: {region}");
    } else {
        println!("Global statistics");
    }
    println!("{}", "=".repeat(40));

    println!("Countries:        {:>12}", s.total_countries);
    println!(
        "Total population: {:>12}",
        format_number(s.total_population)
    );

    if let Some(name) = &s.largest_by_area {
        println!("Largest (area):   {name}");
    }
    if let Some(name) = &s.smallest_by_area {
        println!("Smallest (area):  {name}");
    }
    if let Some(name) = &s.most_populous {
        println!("Most populous:    {name}");
    }
    if let Some(name) = &s.least_populous {
        println!("Least populous:   {name}");
    }

    if args.region.is_none() && !s.regions.is_empty() {
        println!();
        println!(
            "{:<14}  {:>10}  {:>15}",
            "REGION", "COUNTRIES", "POPULATION"
        );
        println!("{}", "-".repeat(44));
        for r in &s.regions {
            println!(
                "{:<14}  {:>10}  {:>15}",
                r.region,
                r.country_count,
                format_number(r.total_population)
            );
        }
    }

    Ok(())
}

fn format_number(n: u64) -> String {
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
