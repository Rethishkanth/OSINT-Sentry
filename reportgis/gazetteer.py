"""An offline gazetteer: place names to coordinates, with no network call.

The plan called for GeoDjango over PostGIS, with geocoding presumably handled
by a service like Nominatim. Neither is available here, and a geocoding API is
not something this project should reach for casually: it means sending every
article's place names to a third party, and it means the module stops working
the moment the key expires or the rate limit bites.

So this is a fixed table, compiled in, and it is honest about being one. It
holds roughly 150 countries and 280 cities - capitals, the largest urban areas,
and the places that turn up in conflict and disaster reporting. That is a tiny
fraction of the world's populated places. A name it does not hold is simply not
found, which is the correct failure: the module says it found nothing rather
than guessing.

Two things this table does that a bigger one often does not:

**It carries deliberate collisions.** Tripoli is in Libya and in Lebanon.
Birmingham is in England and in Alabama. Hyderabad is in India and in Pakistan.
Cambridge, Athens, Alexandria, Naples, Valencia, Portland, London, Manchester,
Córdoba, San Jose and Saint Petersburg all appear twice. These are not
oversights to be cleaned up - they are the cases that decide whether the
geocoder above it is trustworthy, so they are kept and tested.

**Country coordinates are representative interior points, not centroids.** A
country is not a location. Plotting Russia at 61.5N 105.3E says "somewhere in
Russia", and reportgis.geocode marks such a hit LOW confidence for exactly that
reason. Do not read a country pin as an event site.

Coordinates are given to two decimal places, which is about a kilometre. That
is the right precision for a city reference point and more than the rest of the
pipeline can justify.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Kinds, in descending order of how much a hit tells you.
CITY = 'city'
REGION = 'region'
COUNTRY = 'country'


@dataclass(frozen=True)
class Place:
    """One row of the table."""

    name: str
    lat: float
    lon: float
    kind: str
    # ISO 3166-1 alpha-2 of the containing country. A country's own code.
    country: str
    # Sub-national unit, where the table carries one. Used to disambiguate
    # "Springfield, Illinois" from the other Springfields.
    region: str = ''

    @property
    def country_name(self) -> str:
        return COUNTRY_NAMES.get(self.country, self.country)

    @property
    def label(self) -> str:
        """How this place is written when it needs to be unambiguous."""
        parts = [self.name]
        if self.region:
            parts.append(self.region)
        if self.kind != COUNTRY:
            parts.append(self.country_name)
        return ', '.join(parts)


# country code | name | representative lat | lon
_COUNTRIES = """\
AF|Afghanistan|33.90|67.70
AL|Albania|41.15|20.17
DZ|Algeria|28.00|2.60
AO|Angola|-11.20|17.90
AR|Argentina|-38.40|-63.60
AM|Armenia|40.07|45.04
AU|Australia|-25.30|133.80
AT|Austria|47.50|14.55
AZ|Azerbaijan|40.14|47.58
BD|Bangladesh|23.70|90.36
BY|Belarus|53.70|27.95
BE|Belgium|50.50|4.47
BJ|Benin|9.31|2.32
BO|Bolivia|-16.29|-63.59
BA|Bosnia and Herzegovina|43.92|17.68
BW|Botswana|-22.33|24.68
BR|Brazil|-14.24|-51.93
BG|Bulgaria|42.73|25.49
BF|Burkina Faso|12.24|-1.56
BI|Burundi|-3.37|29.92
KH|Cambodia|12.57|104.99
CM|Cameroon|7.37|12.35
CA|Canada|56.13|-106.35
CF|Central African Republic|6.61|20.94
TD|Chad|15.45|18.73
CL|Chile|-35.68|-71.54
CN|China|35.86|104.20
CO|Colombia|4.57|-74.30
CD|Democratic Republic of the Congo|-4.04|21.76
CG|Republic of the Congo|-0.23|15.83
CR|Costa Rica|9.75|-83.75
HR|Croatia|45.10|15.20
CU|Cuba|21.52|-77.78
CY|Cyprus|35.13|33.43
CZ|Czechia|49.82|15.47
DK|Denmark|56.26|9.50
DJ|Djibouti|11.83|42.59
DO|Dominican Republic|18.74|-70.16
EC|Ecuador|-1.83|-78.18
EG|Egypt|26.82|30.80
SV|El Salvador|13.79|-88.90
GQ|Equatorial Guinea|1.65|10.27
ER|Eritrea|15.18|39.78
EE|Estonia|58.60|25.01
ET|Ethiopia|9.15|40.49
FJ|Fiji|-17.71|178.07
FI|Finland|61.92|25.75
FR|France|46.23|2.21
GA|Gabon|-0.80|11.61
GM|Gambia|13.44|-15.31
GE|Georgia|42.32|43.36
DE|Germany|51.17|10.45
GH|Ghana|7.95|-1.02
GR|Greece|39.07|21.82
GT|Guatemala|15.78|-90.23
GN|Guinea|9.95|-9.70
GW|Guinea-Bissau|11.80|-15.18
GY|Guyana|4.86|-58.93
HT|Haiti|18.97|-72.29
HN|Honduras|15.20|-86.24
HU|Hungary|47.16|19.50
IS|Iceland|64.96|-19.02
IN|India|20.59|78.96
ID|Indonesia|-0.79|113.92
IR|Iran|32.43|53.69
IQ|Iraq|33.22|43.68
IE|Ireland|53.41|-8.24
IL|Israel|31.05|34.85
IT|Italy|41.87|12.57
CI|Ivory Coast|7.54|-5.55
JM|Jamaica|18.11|-77.30
JP|Japan|36.20|138.25
JO|Jordan|30.59|36.24
KZ|Kazakhstan|48.02|66.92
KE|Kenya|-0.02|37.91
XK|Kosovo|42.60|20.90
KW|Kuwait|29.31|47.48
KG|Kyrgyzstan|41.20|74.77
LA|Laos|19.86|102.50
LV|Latvia|56.88|24.60
LB|Lebanon|33.85|35.86
LS|Lesotho|-29.61|28.23
LR|Liberia|6.43|-9.43
LY|Libya|26.34|17.23
LT|Lithuania|55.17|23.88
LU|Luxembourg|49.82|6.13
MK|North Macedonia|41.61|21.75
MG|Madagascar|-18.77|46.87
MW|Malawi|-13.25|34.30
MY|Malaysia|4.21|101.98
ML|Mali|17.57|-4.00
MT|Malta|35.94|14.38
MR|Mauritania|21.01|-10.94
MX|Mexico|23.63|-102.55
MD|Moldova|47.41|28.37
MN|Mongolia|46.86|103.85
ME|Montenegro|42.71|19.37
MA|Morocco|31.79|-7.09
MZ|Mozambique|-18.67|35.53
MM|Myanmar|21.91|95.96
NA|Namibia|-22.96|18.49
NP|Nepal|28.39|84.12
NL|Netherlands|52.13|5.29
NZ|New Zealand|-40.90|174.89
NI|Nicaragua|12.87|-85.21
NE|Niger|17.61|8.08
NG|Nigeria|9.08|8.68
KP|North Korea|40.34|127.51
NO|Norway|60.47|8.47
OM|Oman|21.51|55.92
PK|Pakistan|30.38|69.35
PS|Palestine|31.95|35.23
PA|Panama|8.54|-80.78
PG|Papua New Guinea|-6.31|143.96
PY|Paraguay|-23.44|-58.44
PE|Peru|-9.19|-75.02
PH|Philippines|12.88|121.77
PL|Poland|51.92|19.15
PT|Portugal|39.40|-8.22
QA|Qatar|25.35|51.18
RO|Romania|45.94|24.97
RU|Russia|61.52|105.32
RW|Rwanda|-1.94|29.87
SA|Saudi Arabia|23.89|45.08
SN|Senegal|14.50|-14.45
RS|Serbia|44.02|21.01
SL|Sierra Leone|8.46|-11.78
SG|Singapore|1.35|103.82
SK|Slovakia|48.67|19.70
SI|Slovenia|46.15|14.99
SO|Somalia|5.15|46.20
ZA|South Africa|-30.56|22.94
KR|South Korea|35.91|127.77
SS|South Sudan|6.88|31.31
ES|Spain|40.46|-3.75
LK|Sri Lanka|7.87|80.77
SD|Sudan|12.86|30.22
SE|Sweden|60.13|18.64
CH|Switzerland|46.82|8.23
SY|Syria|34.80|39.00
TW|Taiwan|23.70|120.96
TJ|Tajikistan|38.86|71.28
TZ|Tanzania|-6.37|34.89
TH|Thailand|15.87|100.99
TG|Togo|8.62|0.82
TT|Trinidad and Tobago|10.69|-61.22
TN|Tunisia|33.89|9.54
TR|Turkey|38.96|35.24
TM|Turkmenistan|38.97|59.56
UG|Uganda|1.37|32.29
UA|Ukraine|48.38|31.17
AE|United Arab Emirates|23.42|53.85
GB|United Kingdom|55.38|-3.44
US|United States|37.09|-95.71
UY|Uruguay|-32.52|-55.77
UZ|Uzbekistan|41.38|64.59
VE|Venezuela|6.42|-66.59
VN|Vietnam|14.06|108.28
YE|Yemen|15.55|48.52
ZM|Zambia|-13.13|27.85
ZW|Zimbabwe|-19.02|29.15
"""

# name | lat | lon | country | region
#
# Ordered roughly by country. Duplicated names are intentional; see the module
# docstring. Where a name collides, the region column is what lets a phrase
# like "Birmingham, Alabama" resolve.
_CITIES = """\
Kabul|34.53|69.17|AF|
Kandahar|31.62|65.71|AF|
Herat|34.34|62.20|AF|
Tirana|41.33|19.82|AL|
Algiers|36.75|3.06|DZ|
Oran|35.70|-0.63|DZ|
Luanda|-8.84|13.23|AO|
Buenos Aires|-34.60|-58.38|AR|
Rosario|-32.95|-60.64|AR|
Cordoba|-31.42|-64.18|AR|
Yerevan|40.18|44.51|AM|
Sydney|-33.87|151.21|AU|New South Wales
Melbourne|-37.81|144.96|AU|Victoria
Brisbane|-27.47|153.03|AU|Queensland
Perth|-31.95|115.86|AU|Western Australia
Adelaide|-34.93|138.60|AU|South Australia
Canberra|-35.28|149.13|AU|
Vienna|48.21|16.37|AT|
Baku|40.41|49.87|AZ|
Dhaka|23.81|90.41|BD|
Chittagong|22.36|91.78|BD|
Minsk|53.90|27.57|BY|
Brussels|50.85|4.35|BE|
Antwerp|51.22|4.40|BE|
La Paz|-16.50|-68.15|BO|
Santa Cruz de la Sierra|-17.78|-63.18|BO|
Sarajevo|43.86|18.41|BA|
Gaborone|-24.63|25.92|BW|
Sao Paulo|-23.55|-46.63|BR|
Rio de Janeiro|-22.91|-43.17|BR|
Brasilia|-15.79|-47.88|BR|
Salvador|-12.97|-38.50|BR|
Manaus|-3.12|-60.02|BR|
Sofia|42.70|23.32|BG|
Ouagadougou|12.37|-1.52|BF|
Bujumbura|-3.38|29.36|BI|
Phnom Penh|11.56|104.92|KH|
Yaounde|3.85|11.50|CM|
Douala|4.05|9.77|CM|
Toronto|43.65|-79.38|CA|Ontario
Montreal|45.50|-73.57|CA|Quebec
Vancouver|49.28|-123.12|CA|British Columbia
Ottawa|45.42|-75.70|CA|Ontario
Calgary|51.05|-114.07|CA|Alberta
Edmonton|53.55|-113.49|CA|Alberta
London|42.98|-81.25|CA|Ontario
Bangui|4.36|18.56|CF|
N'Djamena|12.11|15.05|TD|
Santiago|-33.45|-70.67|CL|
Valparaiso|-33.05|-71.62|CL|
Beijing|39.90|116.41|CN|
Shanghai|31.23|121.47|CN|
Guangzhou|23.13|113.26|CN|
Shenzhen|22.54|114.06|CN|
Chengdu|30.57|104.07|CN|
Wuhan|30.59|114.31|CN|
Chongqing|29.56|106.55|CN|
Tianjin|39.13|117.20|CN|
Xi'an|34.34|108.94|CN|
Hong Kong|22.32|114.17|CN|
Urumqi|43.83|87.62|CN|Xinjiang
Lhasa|29.65|91.14|CN|Tibet
Bogota|4.71|-74.07|CO|
Medellin|6.24|-75.58|CO|
Cali|3.45|-76.53|CO|
Kinshasa|-4.44|15.27|CD|
Lubumbashi|-11.66|27.48|CD|
Goma|-1.68|29.23|CD|
Bukavu|-2.51|28.86|CD|
Brazzaville|-4.27|15.27|CG|
San Jose|9.93|-84.08|CR|
Zagreb|45.82|15.98|HR|
Havana|23.11|-82.37|CU|
Nicosia|35.19|33.38|CY|
Prague|50.08|14.44|CZ|
Copenhagen|55.68|12.57|DK|
Djibouti City|11.57|43.15|DJ|
Santo Domingo|18.49|-69.93|DO|
Quito|-0.18|-78.47|EC|
Guayaquil|-2.17|-79.92|EC|
Cairo|30.04|31.24|EG|
Alexandria|31.20|29.92|EG|
Giza|30.01|31.21|EG|
San Salvador|13.69|-89.22|SV|
Asmara|15.34|38.93|ER|
Tallinn|59.44|24.75|EE|
Addis Ababa|9.03|38.74|ET|
Mekelle|13.50|39.47|ET|Tigray
Helsinki|60.17|24.94|FI|
Paris|48.86|2.35|FR|
Marseille|43.30|5.37|FR|
Lyon|45.76|4.84|FR|
Toulouse|43.60|1.44|FR|
Nice|43.70|7.27|FR|
Libreville|0.42|9.47|GA|
Tbilisi|41.72|44.79|GE|
Berlin|52.52|13.40|DE|
Munich|48.14|11.58|DE|
Hamburg|53.55|9.99|DE|
Frankfurt|50.11|8.68|DE|
Cologne|50.94|6.96|DE|
Stuttgart|48.78|9.18|DE|
Dusseldorf|51.23|6.78|DE|
Accra|5.60|-0.19|GH|
Athens|37.98|23.73|GR|
Thessaloniki|40.64|22.94|GR|
Guatemala City|14.63|-90.51|GT|
Conakry|9.64|-13.58|GN|
Port-au-Prince|18.59|-72.31|HT|
Tegucigalpa|14.07|-87.19|HN|
Budapest|47.50|19.04|HU|
Reykjavik|64.15|-21.94|IS|
New Delhi|28.61|77.21|IN|Delhi
Mumbai|19.08|72.88|IN|Maharashtra
Bengaluru|12.97|77.59|IN|Karnataka
Kolkata|22.57|88.36|IN|West Bengal
Chennai|13.08|80.27|IN|Tamil Nadu
Hyderabad|17.39|78.49|IN|Telangana
Ahmedabad|23.02|72.57|IN|Gujarat
Pune|18.52|73.86|IN|Maharashtra
Jaipur|26.91|75.79|IN|Rajasthan
Lucknow|26.85|80.95|IN|Uttar Pradesh
Srinagar|34.08|74.80|IN|Kashmir
Jakarta|-6.21|106.85|ID|
Surabaya|-7.25|112.75|ID|
Bandung|-6.92|107.61|ID|
Medan|3.59|98.67|ID|
Tehran|35.69|51.39|IR|
Mashhad|36.30|59.61|IR|
Isfahan|32.65|51.67|IR|
Tabriz|38.08|46.29|IR|
Baghdad|33.31|44.37|IQ|
Mosul|36.34|43.13|IQ|
Basra|30.51|47.78|IQ|
Erbil|36.19|44.01|IQ|Kurdistan
Fallujah|33.35|43.78|IQ|
Dublin|53.35|-6.26|IE|
Jerusalem|31.77|35.21|IL|
Tel Aviv|32.08|34.78|IL|
Haifa|32.79|34.99|IL|
Rome|41.90|12.50|IT|
Milan|45.46|9.19|IT|
Naples|40.85|14.27|IT|
Turin|45.07|7.69|IT|
Palermo|38.12|13.36|IT|
Abidjan|5.36|-4.01|CI|
Kingston|17.97|-76.79|JM|
Tokyo|35.68|139.69|JP|
Osaka|34.69|135.50|JP
Kyoto|35.01|135.77|JP|
Yokohama|35.44|139.64|JP|
Nagoya|35.18|136.91|JP|
Fukuoka|33.59|130.40|JP|
Sapporo|43.06|141.35|JP|
Hiroshima|34.39|132.46|JP|
Amman|31.95|35.93|JO|
Almaty|43.24|76.89|KZ|
Astana|51.17|71.43|KZ|
Nairobi|-1.29|36.82|KE|
Mombasa|-4.04|39.67|KE|
Pristina|42.66|21.16|XK|
Kuwait City|29.38|47.99|KW|
Bishkek|42.87|74.59|KG|
Vientiane|17.97|102.63|LA|
Riga|56.95|24.11|LV|
Beirut|33.89|35.50|LB|
Tripoli|34.44|35.84|LB|
Sidon|33.56|35.37|LB|
Monrovia|6.30|-10.80|LR|
Tripoli|32.89|13.19|LY|
Benghazi|32.12|20.07|LY|
Misrata|32.38|15.09|LY|
Vilnius|54.69|25.28|LT|
Luxembourg City|49.61|6.13|LU|
Skopje|42.00|21.43|MK|
Antananarivo|-18.88|47.51|MG|
Lilongwe|-13.98|33.79|MW|
Kuala Lumpur|3.14|101.69|MY|
Bamako|12.64|-8.00|ML|
Timbuktu|16.77|-3.01|ML|
Valletta|35.90|14.51|MT|
Nouakchott|18.08|-15.98|MR|
Mexico City|19.43|-99.13|MX|
Guadalajara|20.66|-103.35|MX|Jalisco
Monterrey|25.69|-100.32|MX|Nuevo Leon
Tijuana|32.51|-117.04|MX|Baja California
Puebla|19.04|-98.21|MX|
Chisinau|47.01|28.86|MD|
Ulaanbaatar|47.89|106.91|MN|
Podgorica|42.44|19.26|ME|
Rabat|34.02|-6.84|MA|
Casablanca|33.57|-7.59|MA|
Marrakesh|31.63|-8.01|MA|
Maputo|-25.97|32.57|MZ|
Yangon|16.87|96.20|MM|
Mandalay|21.98|96.08|MM|
Naypyidaw|19.75|96.10|MM|
Windhoek|-22.56|17.08|NA|
Kathmandu|27.72|85.32|NP|
Amsterdam|52.37|4.90|NL|
Rotterdam|51.92|4.48|NL|
The Hague|52.08|4.31|NL|
Utrecht|52.09|5.12|NL|
Auckland|-36.85|174.76|NZ|
Wellington|-41.29|174.78|NZ|
Christchurch|-43.53|172.64|NZ|
Managua|12.11|-86.24|NI|
Niamey|13.51|2.11|NE|
Lagos|6.52|3.38|NG|
Abuja|9.06|7.49|NG|
Kano|12.00|8.52|NG|
Port Harcourt|4.82|7.04|NG|
Maiduguri|11.83|13.15|NG|Borno
Pyongyang|39.04|125.76|KP|
Oslo|59.91|10.75|NO|
Bergen|60.39|5.32|NO|
Muscat|23.59|58.41|OM|
Karachi|24.86|67.01|PK|Sindh
Lahore|31.55|74.34|PK|Punjab
Islamabad|33.68|73.05|PK|
Peshawar|34.01|71.58|PK|
Quetta|30.18|66.98|PK|Balochistan
Hyderabad|25.38|68.37|PK|Sindh
Gaza|31.50|34.47|PS|
Ramallah|31.90|35.20|PS|
Hebron|31.53|35.10|PS|
Panama City|8.98|-79.52|PA|
Port Moresby|-9.44|147.18|PG|
Asuncion|-25.28|-57.64|PY|
Lima|-12.05|-77.04|PE|
Arequipa|-16.41|-71.54|PE|
Manila|14.60|120.98|PH|
Cebu|10.32|123.89|PH|
Davao|7.07|125.61|PH|
Warsaw|52.23|21.01|PL|
Krakow|50.06|19.94|PL|
Gdansk|54.35|18.65|PL|
Lisbon|38.72|-9.14|PT|
Porto|41.15|-8.61|PT|
Doha|25.29|51.53|QA|
Bucharest|44.43|26.10|RO|
Moscow|55.76|37.62|RU|
Saint Petersburg|59.93|30.34|RU|
Novosibirsk|55.01|82.93|RU|
Yekaterinburg|56.84|60.61|RU|
Kazan|55.80|49.11|RU|
Vladivostok|43.12|131.89|RU|
Rostov-on-Don|47.23|39.70|RU|
Kigali|-1.94|30.06|RW|
Riyadh|24.71|46.68|SA|
Jeddah|21.49|39.19|SA|
Mecca|21.42|39.83|SA|
Dakar|14.72|-17.47|SN|
Belgrade|44.79|20.45|RS|
Freetown|8.48|-13.23|SL|
Bratislava|48.15|17.11|SK|
Ljubljana|46.06|14.51|SI|
Mogadishu|2.05|45.32|SO|
Hargeisa|9.56|44.07|SO|Somaliland
Johannesburg|-26.20|28.05|ZA|Gauteng
Cape Town|-33.92|18.42|ZA|Western Cape
Durban|-29.86|31.02|ZA|KwaZulu-Natal
Pretoria|-25.75|28.19|ZA|Gauteng
Seoul|37.57|126.98|KR|
Busan|35.18|129.08|KR|
Incheon|37.46|126.71|KR|
Juba|4.86|31.57|SS|
Madrid|40.42|-3.70|ES|
Barcelona|41.39|2.17|ES|Catalonia
Valencia|39.47|-0.38|ES|
Seville|37.39|-5.98|ES|Andalusia
Cordoba|37.89|-4.78|ES|Andalusia
Bilbao|43.26|-2.93|ES|
Colombo|6.93|79.86|LK|
Khartoum|15.50|32.56|SD|
Omdurman|15.65|32.48|SD|
El Fasher|13.63|25.35|SD|Darfur
Stockholm|59.33|18.07|SE|
Gothenburg|57.71|11.97|SE|
Malmo|55.60|13.00|SE|
Zurich|47.37|8.54|CH|
Geneva|46.20|6.14|CH|
Bern|46.95|7.45|CH|
Basel|47.56|7.59|CH|
Damascus|33.51|36.28|SY|
Aleppo|36.20|37.13|SY|
Homs|34.73|36.71|SY|
Idlib|35.93|36.63|SY|
Raqqa|35.95|39.01|SY|
Taipei|25.03|121.57|TW|
Kaohsiung|22.63|120.30|TW|
Dushanbe|38.56|68.79|TJ|
Dar es Salaam|-6.79|39.21|TZ|
Dodoma|-6.16|35.75|TZ|
Bangkok|13.76|100.50|TH|
Chiang Mai|18.79|98.99|TH|
Lome|6.17|1.23|TG|
Port of Spain|10.65|-61.51|TT|
Tunis|36.81|10.18|TN|
Istanbul|41.01|28.98|TR|
Ankara|39.93|32.86|TR|
Izmir|38.42|27.14|TR|
Gaziantep|37.07|37.38|TR|
Diyarbakir|37.91|40.24|TR|
Ashgabat|37.95|58.38|TM|
Kampala|0.35|32.58|UG|
Kyiv|50.45|30.52|UA|
Kharkiv|49.99|36.23|UA|
Odesa|46.48|30.73|UA|
Lviv|49.84|24.03|UA|
Dnipro|48.46|35.05|UA|
Mariupol|47.10|37.54|UA|Donetsk
Bakhmut|48.60|38.00|UA|Donetsk
Dubai|25.20|55.27|AE|
Abu Dhabi|24.45|54.38|AE|
London|51.51|-0.13|GB|England
Manchester|53.48|-2.24|GB|England
Birmingham|52.49|-1.89|GB|England
Glasgow|55.86|-4.25|GB|Scotland
Edinburgh|55.95|-3.19|GB|Scotland
Liverpool|53.41|-2.98|GB|England
Leeds|53.80|-1.55|GB|England
Bristol|51.45|-2.59|GB|England
Sheffield|53.38|-1.47|GB|England
Newcastle|54.98|-1.61|GB|England
Cardiff|51.48|-3.18|GB|Wales
Belfast|54.60|-5.93|GB|Northern Ireland
Cambridge|52.21|0.12|GB|England
Oxford|51.75|-1.26|GB|England
New York|40.71|-74.01|US|New York
Los Angeles|34.05|-118.24|US|California
Chicago|41.88|-87.63|US|Illinois
Houston|29.76|-95.37|US|Texas
Phoenix|33.45|-112.07|US|Arizona
Philadelphia|39.95|-75.17|US|Pennsylvania
San Antonio|29.42|-98.49|US|Texas
San Diego|32.72|-117.16|US|California
Dallas|32.78|-96.80|US|Texas
Austin|30.27|-97.74|US|Texas
San Jose|37.34|-121.89|US|California
San Francisco|37.77|-122.42|US|California
Seattle|47.61|-122.33|US|Washington
Boston|42.36|-71.06|US|Massachusetts
Denver|39.74|-104.99|US|Colorado
Atlanta|33.75|-84.39|US|Georgia
Miami|25.76|-80.19|US|Florida
Detroit|42.33|-83.05|US|Michigan
Las Vegas|36.17|-115.14|US|Nevada
Portland|45.52|-122.68|US|Oregon
Portland|43.66|-70.26|US|Maine
New Orleans|29.95|-90.07|US|Louisiana
Minneapolis|44.98|-93.27|US|Minnesota
Baltimore|39.29|-76.61|US|Maryland
St. Louis|38.63|-90.20|US|Missouri
Pittsburgh|40.44|-80.00|US|Pennsylvania
Cleveland|41.50|-81.69|US|Ohio
Birmingham|33.52|-86.80|US|Alabama
Cambridge|42.37|-71.11|US|Massachusetts
Manchester|42.99|-71.46|US|New Hampshire
Athens|33.96|-83.38|US|Georgia
Alexandria|38.80|-77.05|US|Virginia
Naples|26.14|-81.79|US|Florida
Saint Petersburg|27.77|-82.64|US|Florida
Springfield|39.78|-89.65|US|Illinois
Springfield|37.21|-93.29|US|Missouri
Springfield|42.10|-72.59|US|Massachusetts
Montevideo|-34.90|-56.16|UY|
Tashkent|41.30|69.24|UZ|
Samarkand|39.65|66.98|UZ|
Caracas|10.49|-66.88|VE|
Maracaibo|10.65|-71.64|VE|
Valencia|10.16|-68.00|VE|
Hanoi|21.03|105.85|VN|
Ho Chi Minh City|10.82|106.63|VN|
Da Nang|16.05|108.21|VN|
Sanaa|15.37|44.19|YE|
Aden|12.79|45.04|YE|
Hodeidah|14.80|42.95|YE|
Lusaka|-15.39|28.32|ZM|
Harare|-17.83|31.05|ZW|
Bulawayo|-20.15|28.58|ZW|
"""

# Sub-national units the table knows by name. These earn their place two ways:
# a bare mention is a (low-confidence) location in its own right, and a mention
# alongside a city name is what resolves "Birmingham, Alabama".
_REGIONS = """\
Alabama|32.81|-86.79|US
Alaska|64.20|-149.49|US
Arizona|34.05|-111.09|US
California|36.78|-119.42|US
Colorado|39.06|-105.31|US
Florida|27.66|-81.52|US
Georgia|32.17|-82.90|US
Illinois|40.00|-89.00|US
Louisiana|30.98|-91.96|US
Maine|45.25|-69.45|US
Maryland|39.05|-76.64|US
Massachusetts|42.41|-71.38|US
Michigan|44.31|-85.60|US
Minnesota|46.73|-94.69|US
Missouri|38.46|-92.29|US
Nevada|38.80|-116.42|US
New Hampshire|43.19|-71.57|US
New Mexico|34.52|-105.87|US
North Carolina|35.76|-79.02|US
Ohio|40.42|-82.91|US
Oregon|43.80|-120.55|US
Pennsylvania|41.20|-77.19|US
Tennessee|35.52|-86.58|US
Texas|31.97|-99.90|US
Virginia|37.43|-78.66|US
Washington State|47.75|-120.74|US
Wisconsin|44.50|-89.50|US
Bavaria|48.79|11.50|DE
Catalonia|41.59|1.52|ES
Andalusia|37.54|-4.73|ES
Scotland|56.49|-4.20|GB
Wales|52.13|-3.78|GB
England|52.36|-1.17|GB
Northern Ireland|54.79|-6.49|GB
Ontario|51.25|-85.32|CA
Quebec|52.94|-73.55|CA
British Columbia|53.73|-127.65|CA
Alberta|53.93|-116.58|CA
Kashmir|33.78|76.58|IN
Punjab|31.15|75.34|IN
Tamil Nadu|11.13|78.66|IN
Maharashtra|19.75|75.71|IN
Xinjiang|41.75|85.62|CN
Tibet|31.14|88.78|CN
Donetsk|48.02|37.80|UA
Crimea|45.30|34.15|UA
Darfur|13.00|24.50|SD
Tigray|14.00|38.50|ET
Kurdistan|36.50|44.00|IQ
Borno|11.88|13.15|NG
Sindh|25.89|68.52|PK
Balochistan|28.49|65.10|PK
Gauteng|-26.27|28.11|ZA
Western Cape|-33.23|21.86|ZA
Somaliland|9.55|46.20|SO
Siberia|60.00|100.00|RU
"""

# Names people actually write, mapped to the table's spelling. Accents are
# handled separately by normalisation, so these are the cases normalisation
# cannot reach: abbreviations, exonyms, and former names still in circulation.
ALIASES = {
    'usa': 'United States',
    'u.s.': 'United States',
    'u.s.a.': 'United States',
    'us': 'United States',
    'america': 'United States',
    'united states of america': 'United States',
    'uk': 'United Kingdom',
    'u.k.': 'United Kingdom',
    'britain': 'United Kingdom',
    'great britain': 'United Kingdom',
    'uae': 'United Arab Emirates',
    'drc': 'Democratic Republic of the Congo',
    'dr congo': 'Democratic Republic of the Congo',
    'congo-kinshasa': 'Democratic Republic of the Congo',
    'czech republic': 'Czechia',
    'holland': 'Netherlands',
    'burma': 'Myanmar',
    'cote d ivoire': 'Ivory Coast',
    "cote d'ivoire": 'Ivory Coast',
    'macedonia': 'North Macedonia',
    'washington dc': 'Washington, D.C.',
    'washington d.c.': 'Washington, D.C.',
    'district of columbia': 'Washington, D.C.',
    'new york city': 'New York',
    'nyc': 'New York',
    'la': 'Los Angeles',
    'sf': 'San Francisco',
    'kiev': 'Kyiv',
    'odessa': 'Odesa',
    'bombay': 'Mumbai',
    'calcutta': 'Kolkata',
    'madras': 'Chennai',
    'bangalore': 'Bengaluru',
    'peking': 'Beijing',
    'saigon': 'Ho Chi Minh City',
    'rangoon': 'Yangon',
    'st petersburg': 'Saint Petersburg',
    'st. petersburg': 'Saint Petersburg',
    'leningrad': 'Saint Petersburg',
    'nur-sultan': 'Astana',
    "sana'a": 'Sanaa',
    'gaza city': 'Gaza',
    'gaza strip': 'Gaza',
    'west bank': 'Ramallah',
}


def _fold(text: str) -> str:
    """Case- and accent-insensitive key for a name.

    Sao Paulo, São Paulo and SAO PAULO are one place. Stripping combining marks
    with NFKD is what makes that true without carrying every accented spelling
    in the table.
    """
    decomposed = unicodedata.normalize('NFKD', text)
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return ' '.join(stripped.lower().split())


def _parse(block: str, kind: str) -> list[Place]:
    places = []
    for line in block.strip().splitlines():
        parts = line.split('|')
        if kind == COUNTRY:
            code, name, lat, lon = parts[:4]
            places.append(Place(name, float(lat), float(lon), COUNTRY, code))
        elif kind == REGION:
            name, lat, lon, code = parts[:4]
            places.append(Place(name, float(lat), float(lon), REGION, code, name))
        else:
            name, lat, lon, code = parts[:4]
            region = parts[4] if len(parts) > 4 else ''
            places.append(Place(name, float(lat), float(lon), CITY, code, region))
    return places


COUNTRY_NAMES: dict[str, str] = {
    line.split('|')[0]: line.split('|')[1]
    for line in _COUNTRIES.strip().splitlines()
}

PLACES: list[Place] = (
    _parse(_COUNTRIES, COUNTRY) + _parse(_REGIONS, REGION) + _parse(_CITIES, CITY)
)

# One folded name to every place that carries it. The list is the whole point:
# a name with more than one entry is ambiguous, and the geocoder must be told
# so rather than being handed a silent first-match.
BY_NAME: dict[str, list[Place]] = {}
for _place in PLACES:
    BY_NAME.setdefault(_fold(_place.name), []).append(_place)

def lookup(name: str) -> list[Place]:
    """Every place carrying this name, after alias and accent normalisation.

    An empty list means the table does not have it, which is not the same as
    the place not existing. Callers must not treat a miss as evidence.
    """
    folded = _fold(name)
    folded = _fold(ALIASES.get(folded, folded))
    return list(BY_NAME.get(folded, ()))


# Washington is a city, a US state, and a metonym for a government. All three
# spellings are carried, and the bare one is carried *twice on purpose*: a
# sentence that says only "Washington" has not said which is meant, so the
# geocoder's ambiguity path fires and the analyst is told the name was seen and
# not placed. Dropping the bare form instead would have been silence, which
# reads as "no place was mentioned".
_DC = Place('Washington, D.C.', 38.91, -77.04, CITY, 'US', 'District of Columbia')
for _extra in (
    _DC,
    Place('Washington', 38.91, -77.04, CITY, 'US', 'District of Columbia'),
    Place('Washington', 47.75, -120.74, REGION, 'US', 'Washington'),
):
    PLACES.append(_extra)
    BY_NAME.setdefault(_fold(_extra.name), []).append(_extra)

# Names to scan text for: the table's own, plus the alias spellings, because a
# story writes "the U.S." and "Kiev" far more often than "United States" and
# "Kyiv". lookup() maps an alias back to its row.
_SCANNABLE: list[str] = sorted(set(BY_NAME) | set(ALIASES))

# Longest first, so "Washington, D.C." is tried before "Washington", and
# "New York" before a bare "York" would be if the table carried one.
NAMES_BY_LENGTH: list[str] = sorted(_SCANNABLE, key=len, reverse=True)

# Every name that identifies a country or a region, for resolving the
# "Tripoli, Lebanon" and "Birmingham, Alabama" forms.
QUALIFIERS: dict[str, list[Place]] = {
    name: places
    for name in _SCANNABLE
    if (places := lookup(name)) and any(p.kind in (COUNTRY, REGION) for p in places)
}


# Demonyms, for context only.
#
# This exists because of one sentence in a test run: "A large explosion was
# heard in Tripoli on Monday evening, Libyan officials said." Every human
# reader knows which Tripoli that is. The geocoder refused it, because the
# literal string "Libya" never appears - and refusing a story that has told you
# the answer is not caution, it is a broken reader.
#
# These are used **only** to settle an already-ambiguous name. A demonym never
# places a pin of its own: "American" in an article is not a report from 37N
# 95W, and a table of nationalities would be a terrible gazetteer. The
# inference is also recorded in the pin's reasons and capped at medium
# confidence, so an analyst sees that a nationality, not a place, decided it.
#
# The list is partial, and skewed towards the countries that turn up in
# conflict reporting. A missing demonym costs a refusal, which is the failure
# this module is built to prefer.
DEMONYMS = {
    'afghan': 'Afghanistan', 'albanian': 'Albania', 'algerian': 'Algeria',
    'angolan': 'Angola', 'argentine': 'Argentina', 'argentinian': 'Argentina',
    'armenian': 'Armenia', 'australian': 'Australia', 'austrian': 'Austria',
    'azerbaijani': 'Azerbaijan', 'bangladeshi': 'Bangladesh',
    'belarusian': 'Belarus', 'belgian': 'Belgium', 'bolivian': 'Bolivia',
    'bosnian': 'Bosnia and Herzegovina', 'brazilian': 'Brazil',
    'bulgarian': 'Bulgaria', 'burmese': 'Myanmar', 'cambodian': 'Cambodia',
    'cameroonian': 'Cameroon', 'canadian': 'Canada', 'chadian': 'Chad',
    'chilean': 'Chile', 'chinese': 'China', 'colombian': 'Colombia',
    'congolese': 'Democratic Republic of the Congo', 'croatian': 'Croatia',
    'cuban': 'Cuba', 'cypriot': 'Cyprus', 'czech': 'Czechia',
    'danish': 'Denmark', 'dutch': 'Netherlands', 'ecuadorian': 'Ecuador',
    'egyptian': 'Egypt', 'emirati': 'United Arab Emirates',
    'english': 'England', 'eritrean': 'Eritrea', 'estonian': 'Estonia',
    'ethiopian': 'Ethiopia', 'filipino': 'Philippines', 'finnish': 'Finland',
    'french': 'France', 'gabonese': 'Gabon', 'georgian': 'Georgia',
    'german': 'Germany', 'ghanaian': 'Ghana', 'greek': 'Greece',
    'guatemalan': 'Guatemala', 'haitian': 'Haiti', 'honduran': 'Honduras',
    'hungarian': 'Hungary', 'icelandic': 'Iceland', 'indian': 'India',
    'indonesian': 'Indonesia', 'iranian': 'Iran', 'iraqi': 'Iraq',
    'irish': 'Ireland', 'israeli': 'Israel', 'italian': 'Italy',
    'ivorian': 'Ivory Coast', 'jamaican': 'Jamaica', 'japanese': 'Japan',
    'jordanian': 'Jordan', 'kazakh': 'Kazakhstan', 'kenyan': 'Kenya',
    'kuwaiti': 'Kuwait', 'kyrgyz': 'Kyrgyzstan', 'latvian': 'Latvia',
    'lebanese': 'Lebanon', 'liberian': 'Liberia', 'libyan': 'Libya',
    'lithuanian': 'Lithuania', 'malaysian': 'Malaysia', 'malian': 'Mali',
    'mauritanian': 'Mauritania', 'mexican': 'Mexico', 'moldovan': 'Moldova',
    'mongolian': 'Mongolia', 'moroccan': 'Morocco', 'mozambican': 'Mozambique',
    'namibian': 'Namibia', 'nepalese': 'Nepal', 'nepali': 'Nepal',
    'nicaraguan': 'Nicaragua', 'nigerien': 'Niger', 'nigerian': 'Nigeria',
    'norwegian': 'Norway', 'omani': 'Oman', 'pakistani': 'Pakistan',
    'palestinian': 'Palestine', 'panamanian': 'Panama',
    'paraguayan': 'Paraguay', 'peruvian': 'Peru', 'polish': 'Poland',
    'portuguese': 'Portugal', 'qatari': 'Qatar', 'romanian': 'Romania',
    'russian': 'Russia', 'rwandan': 'Rwanda', 'saudi': 'Saudi Arabia',
    'scottish': 'Scotland', 'senegalese': 'Senegal', 'serbian': 'Serbia',
    'singaporean': 'Singapore', 'slovak': 'Slovakia', 'slovenian': 'Slovenia',
    'somali': 'Somalia', 'spanish': 'Spain', 'sudanese': 'Sudan',
    'swedish': 'Sweden', 'swiss': 'Switzerland', 'syrian': 'Syria',
    'taiwanese': 'Taiwan', 'tajik': 'Tajikistan', 'tanzanian': 'Tanzania',
    'thai': 'Thailand', 'tunisian': 'Tunisia', 'turkish': 'Turkey',
    'turkmen': 'Turkmenistan', 'ugandan': 'Uganda', 'ukrainian': 'Ukraine',
    'uruguayan': 'Uruguay', 'uzbek': 'Uzbekistan', 'venezuelan': 'Venezuela',
    'vietnamese': 'Vietnam', 'welsh': 'Wales', 'yemeni': 'Yemen',
    'zambian': 'Zambia', 'zimbabwean': 'Zimbabwe',
    'american': 'United States', 'british': 'United Kingdom',
}


def demonym_places(word: str) -> list[Place]:
    """The country or region a nationality points at, or an empty list."""
    name = DEMONYMS.get(_fold(word))
    return lookup(name) if name else []


def size() -> dict[str, int]:
    """What the table actually holds, for the module's capability report."""
    return {
        'countries': sum(1 for p in PLACES if p.kind == COUNTRY),
        'regions': sum(1 for p in PLACES if p.kind == REGION),
        'cities': sum(1 for p in PLACES if p.kind == CITY),
        'ambiguous_names': sum(1 for entries in BY_NAME.values() if len(entries) > 1),
    }
