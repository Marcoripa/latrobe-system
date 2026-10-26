/**
 * Geolocation & Haversine formula utility for calculating straight-line distances.
 */

/**
 * Calculates the straight-line distance on a sphere between two coordinate pairs using the Haversine formula.
 *
 * @param {number|null|undefined} lat1 - Latitude of point 1
 * @param {number|null|undefined} lon1 - Longitude of point 1
 * @param {number|null|undefined} lat2 - Latitude of point 2
 * @param {number|null|undefined} lon2 - Longitude of point 2
 * @returns {number} Distance in kilometers, or Infinity if coordinates are invalid/missing.
 */
export const getDistanceKm = (lat1, lon1, lat2, lon2) => {
  if (
    lat1 === null ||
    lat1 === undefined ||
    lon1 === null ||
    lon1 === undefined ||
    lat2 === null ||
    lat2 === undefined ||
    lon2 === null ||
    lon2 === undefined
  ) {
    return Infinity;
  }

  const nLat1 = Number(lat1);
  const nLon1 = Number(lon1);
  const nLat2 = Number(lat2);
  const nLon2 = Number(lon2);

  if (isNaN(nLat1) || isNaN(nLon1) || isNaN(nLat2) || isNaN(nLon2)) {
    return Infinity;
  }

  const R = 6371; // Earth's radius in kilometers
  const dLat = (nLat2 - nLat1) * (Math.PI / 180);
  const dLon = (nLon2 - nLon1) * (Math.PI / 180);
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(nLat1 * (Math.PI / 180)) *
      Math.cos(nLat2 * (Math.PI / 180)) *
      Math.sin(dLon / 2) *
      Math.sin(dLon / 2);

  return R * (2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a)));
};

/**
 * Formats distance in km for human-readable UI display.
 * @param {number} distanceKm
 * @returns {string} e.g. "0.4km" or "4.2km"
 */
export const formatDistanceKm = (distanceKm) => {
  if (distanceKm === null || distanceKm === undefined || !isFinite(distanceKm)) {
    return '';
  }
  if (distanceKm < 0.1) {
    return '< 0.1km';
  }
  return `${distanceKm.toFixed(1)}km`;
};

/**
 * Extracts normalized lat/lng from a project object.
 * Supports `lat`/`lng`, `latitude`/`longitude`, or Firestore GeoPoint objects.
 *
 * @param {object} project
 * @returns {{lat: number|null, lng: number|null}}
 */
export const getProjectCoordinates = (project) => {
  if (!project) return { lat: null, lng: null };

  const lat =
    project.lat ??
    project.latitude ??
    project.coords?.lat ??
    project.location_coords?.lat ??
    project.coordinates?.latitude ??
    (typeof project.location_geopoint?.latitude === 'number'
      ? project.location_geopoint.latitude
      : null);

  const lng =
    project.lng ??
    project.longitude ??
    project.lon ??
    project.coords?.lng ??
    project.location_coords?.lng ??
    project.coordinates?.longitude ??
    (typeof project.location_geopoint?.longitude === 'number'
      ? project.location_geopoint.longitude
      : null);

  return {
    lat: lat !== null && !isNaN(Number(lat)) ? Number(lat) : null,
    lng: lng !== null && !isNaN(Number(lng)) ? Number(lng) : null,
  };
};

/**
 * Attaches `distanceKm` to each project and sorts:
 * - If userCoords provided: closest projects first (ascending distanceKm). Projects without valid coords go to bottom.
 * - If userCoords is null: alphabetical sorting by project name.
 *
 * @param {Array<object>} projects
 * @param {{lat: number, lng: number}|null} userCoords
 * @returns {Array<object>} New sorted array with `distanceKm` attached.
 */
export const sortProjectsWithDistance = (projects, userCoords) => {
  if (!Array.isArray(projects)) return [];

  if (!userCoords || userCoords.lat == null || userCoords.lng == null) {
    return [...projects]
      .map((p) => ({ ...p, distanceKm: Infinity }))
      .sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  }

  return [...projects]
    .map((p) => {
      const { lat, lng } = getProjectCoordinates(p);
      const distanceKm = getDistanceKm(userCoords.lat, userCoords.lng, lat, lng);
      return {
        ...p,
        distanceKm,
      };
    })
    .sort((a, b) => {
      const aDist = a.distanceKm ?? Infinity;
      const bDist = b.distanceKm ?? Infinity;

      // Both have valid finite distances -> sort by distance ascending
      if (isFinite(aDist) && isFinite(bDist)) {
        if (Math.abs(aDist - bDist) > 0.001) {
          return aDist - bDist;
        }
      }

      // One has valid distance, other is Infinity -> valid first
      if (isFinite(aDist) && !isFinite(bDist)) return -1;
      if (!isFinite(aDist) && isFinite(bDist)) return 1;

      // Both lack distance -> alphabetical by name
      return (a.name || '').localeCompare(b.name || '');
    });
};
