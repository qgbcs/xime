package com.kingzcheung.xime.ui.menubar

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowLeft
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.kingzcheung.xime.util.UiOverrides

/**
 * 菜单第五项打开的空白页：顶部保留返回与标题，内容区留空。
 * 标题由 RPC 槽位 ime.menu.feedback#label 控制，未下发时使用“问题反馈”。
 */
@Composable
fun FeedbackPageView(
    backgroundColor: Color,
    textColor: Color,
    accentColor: Color,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val title = UiOverrides.get(UiOverrides.TARGET_MENU_FEEDBACK, "label")
        ?.takeIf { it.isNotBlank() } ?: "问题反馈"
    val iconButtonContainer = androidx.compose.ui.graphics.lerp(
        MaterialTheme.colorScheme.surface,
        MaterialTheme.colorScheme.primary,
        0.35f,
    )
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(backgroundColor),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .height(50.dp)
                .padding(horizontal = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(28.dp)
                    .clip(CircleShape)
                    .background(iconButtonContainer)
                    .clickable { onBack() },
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    imageVector = Icons.AutoMirrored.Filled.KeyboardArrowLeft,
                    contentDescription = "返回",
                    tint = accentColor,
                    modifier = Modifier.size(24.dp),
                )
            }
            Spacer(modifier = Modifier.width(8.dp))
            Text(
                text = title,
                color = textColor,
                style = MaterialTheme.typography.titleMedium,
                maxLines = 1,
            )
        }
        Spacer(modifier = Modifier.weight(1f))
    }
}
