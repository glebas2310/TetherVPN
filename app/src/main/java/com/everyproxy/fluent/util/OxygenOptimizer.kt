package com.everyproxy.fluent.util

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings
import android.widget.Toast

object OxygenOptimizer {

    /**
     * Проверяет, отключена ли оптимизация батареи для нашего приложения.
     */
    fun isBatteryOptimizationIgnored(context: Context): Boolean {
        val powerManager = context.getSystemService(Context.POWER_SERVICE) as? PowerManager ?: return true
        return powerManager.isIgnoringBatteryOptimizations(context.packageName)
    }

    /**
     * Запрашивает стандартный системный диалог исключения из оптимизации батареи.
     */
    @SuppressLint("BatteryLife")
    fun requestIgnoreBatteryOptimization(activity: Activity) {
        if (isBatteryOptimizationIgnored(activity)) {
            Toast.makeText(activity, "Оптимизация батареи уже отключена!", Toast.LENGTH_SHORT).show()
            return
        }

        try {
            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                data = Uri.parse("package:${activity.packageName}")
            }
            activity.startActivity(intent)
        } catch (_: Exception) {
            try {
                val intent = Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
                activity.startActivity(intent)
            } catch (e: Exception) {
                Toast.makeText(activity, "Откройте Настройки -> Батарея вручную", Toast.LENGTH_LONG).show()
            }
        }
    }

    /**
     * Открывает страницу приложения в настройках смартфона (для включения Автозапуска и фоновой работы).
     */
    fun openAppDetailsSettings(context: Context) {
        try {
            val intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                data = Uri.parse("package:${context.packageName}")
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            context.startActivity(intent)
        } catch (e: Exception) {
            Toast.makeText(context, "Не удалось открыть настройки приложения", Toast.LENGTH_SHORT).show()
        }
    }

    /**
     * Рекомендации специально для пользователей OnePlus 9 Pro (OxygenOS 11-14).
     */
    val ONEPLUS_SETUP_STEPS = listOf(
        "1. 🔒 Замочек в недавних: Откройте меню недавних приложений, нажмите на иконку EveryProxy Fluent и выберите «Заблокировать» (Lock).",
        "2. ⚡ Автозапуск: Перейдите в Настройки -> Приложения -> Автозапуск и включите тумблер для EveryProxy Fluent.",
        "3. 🔋 Фоновая активность: В свойствах приложения (Батарея) выберите «Не оптимизировать» и разрешите «Фоновую активность».",
        "4. 💤 Спящий режим: Отключите «Оптимизацию в спящем режиме» в настройках аккумулятора OnePlus, чтобы Wi-Fi не засыпал при выключенном экране."
    )
}
