"""Closed vocabularies shared by every table."""

from enum import StrEnum


class Segment(StrEnum):
    """Visitor segment, as defined by Telefónica's `origen` column."""

    INTERNATIONAL = "international"  # foreign visitors
    NATIONAL = "national"  # live in Spain, outside Barcelona province
    REGIONAL = "regional"  # live in Barcelona province, outside the city
    RESIDENT = "resident"  # live in the municipality of Barcelona


SEGMENT_FROM_RAW = {
    "Internacional": Segment.INTERNATIONAL,
    "Nacional": Segment.NATIONAL,
    "Regional": Segment.REGIONAL,
    "Residente": Segment.RESIDENT,
}


class VisitorType(StrEnum):
    """Telefónica's `tipo_visitante` in the tourism files."""

    TOURIST = "tourist"  # lives outside the province, stayed at least one night
    DAY_TRIPPER = "day_tripper"  # everyone else ("excursionista")


VISITOR_TYPE_FROM_RAW = {"Turista": VisitorType.TOURIST, "Excursionista": VisitorType.DAY_TRIPPER}


class ZoneLevel(StrEnum):
    MUNICIPALITY = "municipality"
    BARRI = "barri"


class SourceFile(StrEnum):
    """Which Telefónica mobility file a trip row came from."""

    BARRI_BARRI = "barri_barri"
    MUNI_MUNI = "muni_muni"
    BARRI_MUNI = "barri_muni"


class TransportMode(StrEnum):
    METRO = "metro"
    BUS = "bus"
    TRAM = "tram"
    FGC = "fgc"
    RODALIES = "rodalies"
    FUNICULAR = "funicular"
    CABLE_CAR = "cable_car"


class HotspotType(StrEnum):
    MONUMENT = "monument"
    MUSEUM = "museum"
    PARK = "park"
    BEACH = "beach"
    STREET = "street"
    MARKET = "market"
    VIEWPOINT = "viewpoint"
    STADIUM = "stadium"
    VENUE = "venue"
    TRANSPORT_HUB = "transport_hub"


class EventType(StrEnum):
    CONGRESS = "congress"
    FESTIVAL = "festival"
    CONCERT = "concert"
    SPORT = "sport"
    CITY_FESTIVAL = "city_festival"
    OTHER = "other"


class FareKind(StrEnum):
    SINGLE = "single"  # one trip
    MULTI_TRIP = "multi_trip"  # e.g. T-casual, 10 trips
    PASS = "pass"  # unlimited trips for N days, e.g. T-usual, T-dia
