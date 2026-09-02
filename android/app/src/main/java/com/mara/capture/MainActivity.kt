package com.mara.capture

import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import com.mara.capture.databinding.ActivityMainBinding

/** Пока каркас: экран здоровья и мастер приезжают следующими нарезками. */
class MainActivity : AppCompatActivity() {
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        val b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)
        b.status.text = "Mara Capture"
    }
}
