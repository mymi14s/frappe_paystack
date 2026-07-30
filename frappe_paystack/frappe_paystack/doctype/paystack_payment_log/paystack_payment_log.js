frappe.ui.form.on('Paystack Payment Log', {
  refresh(frm) {
    frm.trigger('decorate');
    frm.trigger('action_buttons');
  },

  status(frm) {
    frm.trigger('decorate');
  },

  decorate(frm) {
    const s = frm.doc.status || 'Pending';
    const color = {
      'Pending': 'orange',
      'Processed': 'blue',
      'Completed': 'green',
      'Failed': 'red'
    }[s] || 'gray';
    frm.dashboard.clear_headline();
    frm.dashboard.set_headline_alert(
      __(`Status: <strong style="text-transform:uppercase">${s}</strong>`), color
    );
  },
  action_buttons(frm) {
    if (!frm.doc.transaction_id) return;

    frm.add_custom_button(__('Open in Paystack'), () => {
      const url = `https://dashboard.paystack.com/#/transactions/${frm.doc.transaction_id}/analytics`;
      window.open(url, '_blank');
    }, __('Actions'));

    frm.add_custom_button(__('Verify Transaction'), () => {
      frm.call("validate_payment").then(res=>{
        if (res.message.message) {
          frappe.msgprint(res.message.message);
        }
      })
    }, __('Actions'));
  }
});
