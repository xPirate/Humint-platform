package org.humint.field.data

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Looper
import androidx.core.content.ContextCompat
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * Where the phone thinks it is.
 *
 * android.location.LocationManager, not FusedLocationProvider. Fused is part
 * of Google Play Services, and these handsets are off-network and may be
 * de-Googled; an app that cannot get a fix without GMS is an app that gets
 * no fix at all on the fleet it was built for. Raw GPS is also the right
 * answer on the merits here: there is no network to fuse with.
 *
 * Listening runs only while a report is open on screen. There is no
 * background location permission and no foreground service, deliberately —
 * a reporting tool that also tracks the analyst is a different product, and
 * a worse one to have on a phone that might be searched.
 */
class Position(private val context: Context) : LocationListener {

    data class Fix(val lat: Double, val lng: Double, val accuracyM: Float, val ageMs: Long) {
        /** A phone indoors will happily report a two-minute-old fix from the
         *  car park. Worth knowing, so it is shown rather than hidden. */
        val stale: Boolean get() = ageMs > 120_000
    }

    private val _fix = MutableStateFlow<Fix?>(null)
    val fix: StateFlow<Fix?> = _fix

    private val manager: LocationManager? =
        ContextCompat.getSystemService(context, LocationManager::class.java)

    fun hasPermission(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED ||
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    @SuppressLint("MissingPermission")
    fun start() {
        val manager = manager ?: return
        if (!hasPermission()) return
        // Seed from the last known fix so the form has something to show
        // immediately; the first real GPS fix can take half a minute under
        // trees, and an empty position box invites the analyst to give up on
        // it and move on.
        listOfNotNull(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
            .filter { runCatching { manager.isProviderEnabled(it) }.getOrDefault(false) }
            .also { providers ->
                providers.mapNotNull { runCatching { manager.getLastKnownLocation(it) }.getOrNull() }
                    .maxByOrNull { it.time }
                    ?.let { onLocationChanged(it) }
                providers.forEach { provider ->
                    runCatching {
                        manager.requestLocationUpdates(provider, 2_000L, 0f, this,
                                                       Looper.getMainLooper())
                    }
                }
            }
    }

    fun stop() {
        runCatching { manager?.removeUpdates(this) }
    }

    override fun onLocationChanged(location: Location) {
        val candidate = Fix(
            lat = location.latitude,
            lng = location.longitude,
            accuracyM = if (location.hasAccuracy()) location.accuracy else Float.MAX_VALUE,
            ageMs = (System.currentTimeMillis() - location.time).coerceAtLeast(0),
        )
        val current = _fix.value
        // Keep the better fix rather than the latest one: GPS and network
        // arrive interleaved, and a 1500-metre network fix landing after a
        // 5-metre GPS fix should not replace it.
        if (current == null || candidate.accuracyM <= current.accuracyM || current.stale) {
            _fix.value = candidate
        }
    }

    @Deprecated("Required by LocationListener on API < 30; never called on the fleet.")
    override fun onStatusChanged(provider: String?, status: Int, extras: android.os.Bundle?) = Unit
    override fun onProviderEnabled(provider: String) = Unit
    override fun onProviderDisabled(provider: String) = Unit
}
