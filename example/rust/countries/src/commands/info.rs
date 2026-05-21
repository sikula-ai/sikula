use crate::cli::InfoArgs;
use countries::domain::country::Country;
use countries::domain::filter;
use countries::error::AppError;

pub fn run(args: &InfoArgs, countries: &[Country]) -> Result<(), AppError> {
    let country = filter::find_by_code(countries, &args.code)
        .ok_or_else(|| AppError::CountryNotFound(args.code.to_string()))?;

    let label_w = 16;

    println!("{:<label_w$} {}", "Name:", country.name.common);
    println!("{:<label_w$} {}", "Official:", country.name.official);
    println!("{:<label_w$} {}", "Code (alpha-3):", country.cca3);
    println!("{:<label_w$} {}", "Capital:", country.capital.join(", "));
    println!("{:<label_w$} {}", "Region:", country.region);
    println!("{:<label_w$} {}", "Subregion:", country.subregion);
    println!(
        "{:<label_w$} {}",
        "Population:",
        format_number(country.population)
    );
    println!(
        "{:<label_w$} {} km²",
        "Area:",
        format_number(country.area as u64)
    );
    println!("{:<label_w$} {}", "Languages:", country.language_names());
    println!("{:<label_w$} {}", "Currencies:", country.currency_summary());

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
