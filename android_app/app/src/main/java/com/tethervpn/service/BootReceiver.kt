package com.tethervpn.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action == Intent.ACTION_BOOT_COMPLETED ||
            intent?.action == "android.intent.action.QUICKBOOT_POWERON"
        ) {
            val prefs = context.getSharedPreferences("tethervpn_prefs", Context.MODE_PRIVATE)
            val autoStart = prefs.getBoolean("autostart_on_boot", false)
            val port = prefs.getInt("proxy_port", 8080)
            if (autoStart) {
                ProxyForegroundService.startService(context, port)
            }
        }
    }
}
